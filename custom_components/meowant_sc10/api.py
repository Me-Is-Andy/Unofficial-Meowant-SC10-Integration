"""Tuya Cloud API client using the post-2021 signature algorithm."""
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    DEVICE_LIST_PATH,
    TOKEN_PATH,
    command_path,
    device_info_path,
    status_path,
)

_LOGGER = logging.getLogger(__name__)

EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


class TuyaApiError(Exception):
    """Raised when the Tuya Cloud API returns an error."""


class TuyaCloudApi:
    """Minimal Tuya Cloud client: token management + device read/write."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        device_id: str,
        access_id: str,
        access_secret: str,
    ):
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._device_id = device_id
        self._access_id = access_id
        self._access_secret = access_secret
        self._access_token = None
        self._refresh_token = None
        self._token_expires_at = None

    @staticmethod
    def _now_ms() -> str:
        return str(int(dt_util.utcnow().timestamp() * 1000))

    def _sign(self, timestamp: str, method: str, path: str, body: str, access_token: str) -> str:
        """Build the signature per Tuya's current algorithm.

        stringToSign = METHOD \n SHA256(body) \n <optional headers> \n path
        str          = client_id + access_token + t + nonce + stringToSign
        sign         = HMAC-SHA256(str, secret).upper()
        """
        content_sha256 = hashlib.sha256(body.encode()).hexdigest() if body else EMPTY_BODY_SHA256
        string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
        payload = f"{self._access_id}{access_token}{timestamp}{string_to_sign}"
        return hmac.new(
            self._access_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

    async def _request(self, method: str, path: str, body: dict | None = None, use_token: bool = True) -> dict:
        timestamp = self._now_ms()
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        token = self._access_token if use_token else ""

        if use_token and not token:
            raise TuyaApiError("No access token available")

        headers = {
            "client_id": self._access_id,
            "sign_method": "HMAC-SHA256",
            "t": timestamp,
            "sign": self._sign(timestamp, method, path, body_str, token),
            "Content-Type": "application/json",
        }
        if use_token:
            headers["access_token"] = token

        url = f"{self._base_url}{path}"
        async with self._session.request(
            method, url, headers=headers, data=body_str or None, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            data = await resp.json()

        if not data.get("success"):
            raise TuyaApiError(f"{data.get('code')}: {data.get('msg')}")
        return data

    async def _request_with_token_retry(self, method: str, path: str, body: dict | None = None) -> dict:
        """Run a request, refetching the token once if it is rejected."""
        await self.async_ensure_token()
        try:
            return await self._request(method, path, body=body)
        except TuyaApiError as err:
            if "1010" in str(err) or "token" in str(err).lower():
                _LOGGER.debug("Token rejected, refetching and retrying")
                await self.async_fetch_token()
                return await self._request(method, path, body=body)
            raise

    async def async_ensure_token(self) -> None:
        """Fetch a token if we don't have a valid one."""
        if self._access_token and self._token_expires_at and dt_util.utcnow() < self._token_expires_at:
            return
        await self.async_fetch_token()

    async def async_fetch_token(self) -> None:
        """Get an access token.

        Tuya's expire_time is the REMAINING life of the token it returns, not a
        fresh lifetime, and it hands back the same token until that runs out.
        Renewing early therefore just re-fetches the same nearly-dead token in a
        loop, so run it to expiry instead: the request that trips a 1010 is
        retried with a new token by the callers below.
        """
        data = await self._request("GET", TOKEN_PATH, use_token=False)
        result = data.get("result", {})
        self._access_token = result.get("access_token")
        self._refresh_token = result.get("refresh_token")
        remaining = result.get("expire_time") or result.get("expires_in") or 7200
        self._token_expires_at = dt_util.utcnow() + timedelta(seconds=max(int(remaining), 10))
        _LOGGER.debug("Tuya access token acquired, %ss remaining", remaining)

    async def async_list_devices(self) -> list[dict]:
        """Return every device in the cloud project.

        Used by the config flow: this is where the local key comes from, and it
        carries friendly names so the user can recognise their device. The
        response includes local keys for every device in the project, so it
        must never be logged or written to diagnostics.
        """
        data = await self._request_with_token_retry("GET", DEVICE_LIST_PATH)
        result = data.get("result", [])
        return result if isinstance(result, list) else []

    async def async_get_properties(self) -> dict[int, dict]:
        """Return {dp_id: {"value": ..., "time": ...}} for the device.

        The per-property timestamp matters: it is the only reliable way to
        detect a repeated event whose value happens to be unchanged.
        """
        data = await self._request_with_token_retry("GET", status_path(self._device_id))

        properties = data.get("result", {}).get("properties", [])
        values: dict[int, dict] = {}
        for prop in properties:
            dp_id = prop.get("dp_id")
            if dp_id is not None and "value" in prop:
                values[dp_id] = {"value": prop["value"], "time": prop.get("time")}
        return values

    async def async_get_device_info(self) -> dict:
        """Return the device record, including online status and active_time.

        Note that active_time is the activation timestamp, not the last
        connection: it does not move when the device is power cycled.
        """
        data = await self._request_with_token_retry("GET", device_info_path(self._device_id))
        return data.get("result", {})

    async def async_send_command(self, code: str, value) -> None:
        """Send a single command to the device."""
        body = {"commands": [{"code": code, "value": value}]}
        await self._request_with_token_retry("POST", command_path(self._device_id), body=body)
