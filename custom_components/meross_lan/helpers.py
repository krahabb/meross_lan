"""
    Helpers!
"""
from __future__ import annotations
import asyncio
from logging import getLogger
from functools import partial
from time import time
import typing

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.util.dt import utcnow

from .const import DOMAIN
from .merossclient import const as mc


try:
    from homeassistant.backports.enum import StrEnum # type: ignore pylint: disable=unused-import
except:
    import enum
    class StrEnum(enum.Enum):
        """
        convenience alias for homeassistant.backports.StrEnum
        """
        def __str__(self):
            return str(self.value)


if typing.TYPE_CHECKING:
    from typing import Callable, Coroutine
    from homeassistant.core import HomeAssistant, State

LOGGER = getLogger(__name__[:-8])  # get base custom_component name for logging
_TRAP_DICT = {}


def LOGGER_trap(level, timeout, msg, *args):
    """
    avoid repeating the same last log message until something changes or timeout expires
    used mainly when discovering new devices
    """
    global _TRAP_DICT

    epoch = time()
    trap_key = (msg, *args)
    trap_time = _TRAP_DICT.get(trap_key, 0)
    if (epoch - trap_time) < timeout:
        return

    LOGGER.log(level, msg, *args)
    _TRAP_DICT[trap_key] = epoch


def clamp(_value, _min, _max):
    """
    saturate _value between _min and _max
    """
    if _value >= _max:
        return _max
    elif _value <= _min:
        return _min
    else:
        return _value


def reverse_lookup(_dict: dict, value):
    """
    lookup the values in map (dict) and return
    the corresponding key
    """
    for _key, _value in _dict.items():
        if _value == value:
            return _key
    return None


def versiontuple(version: str):
    """
    helper for version checking, comparisons, etc
    """
    return tuple(map(int, (version.split("."))))


"""
    obfuscation:
    call obfuscate on a paylod (dict) to remove well-known sensitive
    keys (list in OBFUSCATE_KEYS). The returned dictionary contains a
    copy of original values and need to be used a gain when calling
    deobfuscate on the previously obfuscated payload
"""
OBFUSCATE_KEYS = (
    mc.KEY_UUID,
    mc.KEY_MACADDRESS,
    mc.KEY_WIFIMAC,
    mc.KEY_INNERIP,
    mc.KEY_SERVER,
    mc.KEY_PORT,
    mc.KEY_SECONDSERVER,
    mc.KEY_SECONDPORT,
    mc.KEY_USERID,
    mc.KEY_TOKEN,
)

def obfuscate(payload: dict):
    """
    payload: input-output gets modified by blanking sensistive keys
    returns: a dict with the original mapped obfuscated keys
    parses the input payload and 'hides' (obfuscates) some sensitive keys.
    returns the mapping of the obfuscated keys in 'obfuscated' so to re-set them in _deobfuscate
    this function is recursive
    """
    obfuscated = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            o = obfuscate(value)
            if o:
                obfuscated[key] = o
        elif key in OBFUSCATE_KEYS:
            obfuscated[key] = value
            payload[key] = "#" * len(str(value))

    return obfuscated


def deobfuscate(payload: dict, obfuscated: dict):
    for key, value in obfuscated.items():
        if isinstance(value, dict):
            deobfuscate(payload[key], value)
        else:
            payload[key] = value


def schedule_async_callback(
    hass: HomeAssistant, delay: float, target: Callable[..., Coroutine], *args
) -> asyncio.TimerHandle:
    @callback
    def _callback(_target, *_args):
        hass.async_create_task(_target(*_args))

    return hass.loop.call_later(delay, _callback, target, *args)


def schedule_callback(
    hass: HomeAssistant, delay: float, target: Callable, *args
) -> asyncio.TimerHandle:
    return hass.loop.call_later(delay, target, *args)


"""
RECORDER helpers
"""
async def get_entity_last_states(
    hass: HomeAssistant, number_of_states: int, entity_id: str
) -> list[State] | None:
    """
    recover the last known good state from recorder in order to
    restore transient state information when restarting HA
    """
    from homeassistant.components.recorder import history

    if hasattr(history, "get_state"):  # removed in 2022.6.x
        return history.get_state(hass, utcnow(), entity_id)  # type: ignore

    elif hasattr(history, "get_last_state_changes"):
        """
        get_instance too is relatively new: I hope it was in place when
        get_last_state_changes was added
        """
        from homeassistant.components.recorder import get_instance

        _last_state = await get_instance(hass).async_add_executor_job(
            partial(
                history.get_last_state_changes,
                hass,
                number_of_states,
                entity_id,
            )
        )
        return _last_state.get(entity_id)

    else:
        raise Exception("Cannot find history.get_last_state_changes api")

async def get_entity_last_state(hass: HomeAssistant, entity_id: str) -> State | None:
    if states := await get_entity_last_states(hass, 1, entity_id):
        return states[0]
    return None

async def get_entity_last_state_available(hass: HomeAssistant, entity_id: str) -> State | None:
    """
    if the device/entity was disconnected before restarting and we need
    the last good reading from the device, we need to skip the last
    state since it is 'unavailable'
    """
    states = await get_entity_last_states(hass, 2, entity_id)
    if states is not None:
        for state in reversed(states):
            if state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                return state
    return None


class ConfigEntriesHelper:

    def __init__(self, hass: HomeAssistant):
        self.config_entries = hass.config_entries
        self.entries = hass.config_entries.async_entries(DOMAIN)
        self.flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    def get_config_entry(self, unique_id: str):
        for config_entry in self.entries:
            if config_entry.unique_id == unique_id:
                return config_entry
        return None

    def get_config_flow(self, unique_id: str):
        for flow in self.flows:
            if context := flow.get("context"):
                if context.get("unique_id") == unique_id:
                    return flow
        return None
