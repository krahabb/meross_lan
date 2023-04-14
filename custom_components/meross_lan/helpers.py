"""
    Helpers!
"""
from __future__ import annotations

import abc
import asyncio
from contextlib import contextmanager
from functools import partial
import logging
from time import time
import typing

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.util.dt import utcnow

from .const import DOMAIN
from .merossclient import const as mc

try:
    from homeassistant.backports.enum import (
        StrEnum,  # type: ignore pylint: disable=unused-import
    )
except:
    import enum

    class StrEnum(enum.Enum):
        """
        convenience alias for homeassistant.backports.StrEnum
        """

        def __str__(self):
            return str(self.value)


if typing.TYPE_CHECKING:
    from typing import Callable, ClassVar, Coroutine

    from homeassistant.core import HomeAssistant, State

    from . import MerossApi
    from .meross_device import MerossDevice
    from .meross_profile import MerossCloudProfile
    from .merossclient.cloudapi import MerossCloudCredentials


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
    LOGGER:
    Customize logging library to 'trap' repeated messages under a
    short timeout
"""


def getLogger(name):
    """
    Replaces the default Logger with our wrapped implementation:
    replace your logging.getLogger with helpers.getLogger et voilà
    """
    logger = logging.getLogger(name)
    logger.__class__ = type(
        "Logger",
        (
            _Logger,
            logger.__class__,
        ),
        {},
    )
    return logger


class _Logger(logging.Logger if typing.TYPE_CHECKING else object):
    """
    This wrapper will 'filter' log messages and avoid
    verbose over-logging for the same message by using a timeout
    to prevent repeating the very same log before the timeout expires.
    The implementation 'hacks' a standard Logger instance by mixin-ing
    """

    # default timeout: these can be overriden at the log call level
    # by passing in the 'timeout=' param
    # for example: LOGGER.error("This error will %s be logged again", "soon", timeout=5)
    # it can also be overriden at the 'Logger' instance level
    default_timeout = 60 * 60 * 8
    # cache of logged messages with relative last-thrown-epoch
    _LOGGER_TIMEOUTS = {}

    def _log(self, level, msg, args, **kwargs):
        if "timeout" in kwargs:
            timeout = kwargs.pop("timeout")
            epoch = time()
            trap_key = (msg, args)
            if trap_key in _Logger._LOGGER_TIMEOUTS:
                if (epoch - _Logger._LOGGER_TIMEOUTS[trap_key]) < timeout:
                    if self.isEnabledFor(logging.DEBUG):
                        super()._log(
                            logging.DEBUG,
                            f"dropped log message for {msg}",
                            args,
                            **kwargs,
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch

        super()._log(level, msg, args, **kwargs)


LOGGER = getLogger(__name__[:-8])  # get base custom_component name for logging


class Loggable:
    """
    Helper base class for logging instance name/id related info.
    Derived classes can customize this in different flavours:
    Basic is to override 'logtag' to provide a custom name when
    logging. By overriding 'log' (together with 'warning' since
    the basic implementation doesnt call 'log' in order to optimize/skip
    a call and forwards itself the log message to the underlying LOGGER)
    like in 'MerossDevice' we can intercept log messages
    Here, since most of our logs are WARNINGS, the 'warning' interface
    is just a small optimization in order to reduce parameters pushing
    """

    @property
    def logtag(self):
        return self.__class__.__name__

    def log(self, level: int, msg: str, *args, **kwargs):
        LOGGER.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        LOGGER.warning(f"{self.logtag}: {msg}", *args, **kwargs)

    def log_exception(
        self, level: int, exception: Exception, msg: str, *args, **kwargs
    ):
        self.log(
            level,
            f"{exception.__class__.__name__}({str(exception)}) in {msg}",
            *args,
            **kwargs,
        )

    def log_exception_warning(self, exception: Exception, msg: str, *args, **kwargs):
        self.warning(
            f"{exception.__class__.__name__}({str(exception)}) in {msg}",
            *args,
            **kwargs,
        )

    @contextmanager
    def exception_warning(self, msg: str, *args, **kwargs):
        try:
            yield
        except Exception as exception:
            self.warning(
                f"{exception.__class__.__name__}({str(exception)}) in {msg}",
                *args,
                **kwargs,
            )


@contextmanager
def log_exceptions(logger: logging.Logger = LOGGER):
    try:
        yield
    except Exception as error:
        logger.error("Unexpected %s: %s", type(error).__name__, str(error))


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


async def get_entity_last_state_available(
    hass: HomeAssistant, entity_id: str
) -> State | None:
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
        self.config_entries: typing.Final = hass.config_entries
        self._entries = None
        self._flows = None

    def get_config_entry(self, unique_id: str):
        if self._entries is None:
            self._entries = self.config_entries.async_entries(DOMAIN)
        for config_entry in self._entries:
            if config_entry.unique_id == unique_id:
                return config_entry
        return None

    def get_config_flow(self, unique_id: str):
        if self._flows is None:
            self._flows = self.config_entries.flow.async_progress_by_handler(DOMAIN)
        for flow in self._flows:
            if context := flow.get("context"):
                if context.get("unique_id") == unique_id:
                    return flow
        return None


class ApiProfile(Loggable, abc.ABC):
    """
    base class for both MerossCloudProfile and MerossApi
    allowing lightweight sharing of globals and defining
    a common interface
    """

    # hass, api: set when initializing MerossApi
    hass: ClassVar[HomeAssistant] = None  # type: ignore
    api: ClassVar[MerossApi] = None  # type: ignore
    # devices: list of known devices. Every device config_entry
    # in the system is mapped here and set to the MerossDevice instance
    # if the device is actually active (config_entry loaded) or None
    # if the device entry is not loaded
    devices: ClassVar[dict[str, MerossDevice | None]] = {}

    @staticmethod
    def active_devices():
        return (device for device in ApiProfile.devices.values() if device is not None)

    # profiles: list of known cloud profiles (same as devices)
    profiles: ClassVar[dict[str, MerossCloudProfile | None]] = {}

    @staticmethod
    def active_profiles():
        return (
            profile for profile in ApiProfile.profiles.values() if profile is not None
        )

    # instance attributes
    @property
    @abc.abstractmethod
    def key(self) -> str | None:
        return None

    @staticmethod
    def get_device_with_mac(macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in ApiProfile.active_devices():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    @staticmethod
    async def async_update_profile(credentials: MerossCloudCredentials):
        profile_id = credentials[mc.KEY_USERID_]
        profile = ApiProfile.profiles.get(profile_id)
        if profile is not None:
            await profile.async_update_credentials(credentials)
