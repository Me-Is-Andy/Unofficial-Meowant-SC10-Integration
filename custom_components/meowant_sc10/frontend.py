"""Serve and register the Lovelace card that ships with this integration.

Registration is deliberately fail-soft: a problem serving the card must never
take the integration's entities down with it.
"""
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Bump this whenever meowant-card.js changes, or browsers will serve a stale
# copy from cache.
CARD_VERSION = "1.0.2"
CARD_FILENAME = "meowant-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"

_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card file and tell the frontend to load it."""
    if hass.data.get(_REGISTERED):
        return

    card_path = Path(__file__).parent / "www" / CARD_FILENAME
    if not card_path.is_file():
        _LOGGER.warning("Card file missing at %s; skipping registration", card_path)
        return

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(card_path), True)]
        )
        add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    except Exception:
        _LOGGER.exception("Could not register the Lovelace card; entities are unaffected")
        return

    hass.data[_REGISTERED] = True
    _LOGGER.debug("Registered Lovelace card at %s", CARD_URL)
