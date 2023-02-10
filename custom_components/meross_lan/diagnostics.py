from __future__ import annotations
import typing
from copy import deepcopy
from homeassistant.components.diagnostics import REDACTED

from . import MerossApi
from .helpers import obfuscate
from .const import (
    CONF_DEVICE_ID, CONF_PAYLOAD,
    CONF_HOST, CONF_KEY, CONF_CLOUD_KEY,
    CONF_PROTOCOL, CONF_POLLING_PERIOD,
    CONF_TRACE, CONF_TRACE_TIMEOUT,
)

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_get_config_entry_diagnostics(
    hass, entry: 'ConfigEntry'
) -> dict[str, object]:
    """Return diagnostics for a config entry."""
    return await _async_get_diagnostics(hass, entry)


async def async_get_device_diagnostics(
    hass, entry: 'ConfigEntry', device
) -> dict[str, object]:
    """Return diagnostics for a device entry."""
    return await _async_get_diagnostics(hass, entry)


async def _async_get_diagnostics(hass, entry: 'ConfigEntry'):

    device_id = entry.data.get(CONF_DEVICE_ID)
    if device_id is None:# MQTT hub entry
        return {
            CONF_KEY: REDACTED if entry.data.get(CONF_KEY) else None,
            "disabled_by": entry.disabled_by,
            "disabled_polling": entry.pref_disable_polling,
        }

    device = MerossApi.peek_device(hass, device_id)
    deviceclass = type(device).__name__ if device is not None else None
    trace_timeout = entry.data.get(CONF_TRACE_TIMEOUT)
    payload = deepcopy(entry.data.get(CONF_PAYLOAD)) #copy to avoid obfuscating entry.data
    obfuscate(payload) # type: ignore

    data = {
        CONF_HOST: REDACTED if entry.data.get(CONF_HOST) else None,
        CONF_KEY: REDACTED if entry.data.get(CONF_KEY) else None,
        CONF_CLOUD_KEY: REDACTED if entry.data.get(CONF_CLOUD_KEY) else None,
        CONF_PROTOCOL: entry.data.get(CONF_PROTOCOL),
        CONF_POLLING_PERIOD: entry.data.get(CONF_POLLING_PERIOD),
        CONF_TRACE_TIMEOUT: trace_timeout,
        CONF_DEVICE_ID: REDACTED,
        CONF_PAYLOAD: payload,
        "deviceclass": deviceclass,
        "disabled_by": entry.disabled_by,
        "disabled_polling": entry.pref_disable_polling,
        CONF_TRACE: (await device.get_diagnostics_trace(trace_timeout)) if device is not None else None
    }

    return data