"""
    Helpers!
"""
from __future__ import annotations

import abc
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
import logging
from time import gmtime, time
import typing

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.util import dt as dt_util

from .. import const as mlc

if typing.TYPE_CHECKING:
    from datetime import tzinfo
    from typing import Callable, Coroutine, Final

    from homeassistant.core import HomeAssistant, State

    from .. import MerossApi


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


def utcdatetime_from_epoch(epoch):
    """
    converts an epoch (UTC seconds) in a non-naive datetime.
    Faster than datetime.fromtimestamp with less checks
    and no care for milliseconds
    """
    y, m, d, hh, mm, ss, weekday, jday, dst = gmtime(epoch)
    return datetime(y, m, d, hh, mm, min(ss, 59), 0, timezone.utc)


def datetime_from_epoch(epoch, tz: tzinfo | None = None):
    """
    converts an epoch (UTC seconds) in a non-naive datetime.
    Faster than datetime.fromtimestamp with less checks
    and no care for milliseconds
    """
    y, m, d, hh, mm, ss, weekday, jday, dst = gmtime(epoch)
    utcdt = datetime(y, m, d, hh, mm, min(ss, 59), 0, timezone.utc)
    return (
        utcdt
        if tz is timezone.utc
        else utcdt.astimezone(tz or dt_util.DEFAULT_TIME_ZONE)
    )


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


async def get_entity_last_states(
    hass: HomeAssistant, number_of_states: int, entity_id: str
) -> list[State] | None:
    """
    recover the last known good state from recorder in order to
    restore transient state information when restarting HA
    """
    from homeassistant.components.recorder import history

    if hasattr(history, "get_state"):  # removed in 2022.6.x
        return history.get_state(hass, dt_util.utcnow(), entity_id)  # type: ignore

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
    if states := await get_entity_last_states(hass, 2, entity_id):
        for state in reversed(states):
            if state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                return state
    return None


class ConfigEntryType(StrEnum):
    UNKNOWN = "unknown"
    DEVICE = "device"
    PROFILE = "profile"
    HUB = "hub"

    @staticmethod
    def get_type_and_id(unique_id: str | None):
        match (unique_id or ".").split("."):
            case (mlc.DOMAIN,):
                return (ConfigEntryType.HUB, None)
            case (device_id,):
                return (ConfigEntryType.DEVICE, device_id)
            case ("profile", profile_id):
                return (ConfigEntryType.PROFILE, profile_id)
            case _:
                return (ConfigEntryType.UNKNOWN, None)


class ConfigEntriesHelper:
    __slots__ = (
        "config_entries",
        "_entries",
        "_async_entry_for_domain_unique_id",
    )

    def __init__(self, hass: HomeAssistant):
        self.config_entries: typing.Final = hass.config_entries
        self._entries = None
        # added in HA core 2024.2
        self._async_entry_for_domain_unique_id = getattr(self.config_entries, "async_entry_for_domain_unique_id", None)

    def get_config_entry(self, unique_id: str):
        """Gets the configured entry if it exists."""
        if self._async_entry_for_domain_unique_id:
            return self._async_entry_for_domain_unique_id(mlc.DOMAIN, unique_id)
        if self._entries is None:
            self._entries = self.config_entries.async_entries(mlc.DOMAIN)
        for config_entry in self._entries:
            if config_entry.unique_id == unique_id:
                return config_entry
        return None

    def get_config_flow(self, unique_id: str):
        """Returns the current flow (in progres) if any."""
        for progress in self.config_entries.flow.async_progress_by_handler(
            mlc.DOMAIN,
            include_uninitialized=True,
            match_context={"unique_id": unique_id},
        ):
            return progress
        return None


def getLogger(name):
    """
    Replaces the default Logger with our wrapped implementation:
    replace your logging.getLogger with helpers.getLogger et voilà
    """
    logger = logging.getLogger(name)
    # watchout: getLogger could return an instance already
    # subclassed if we previously asked for the same name
    # for example when we reload a config entry
    _class = logger.__class__
    if _class not in _Logger._CLASS_HOOKS.values():
        # getLogger returned a 'virgin' class
        if _class in _Logger._CLASS_HOOKS.keys():
            # we've alread subclassed this type, so we reuse it
            logger.__class__ = _Logger._CLASS_HOOKS[_class]
        else:
            logger.__class__ = _Logger._CLASS_HOOKS[_class] = type(
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
    # cache of subclassing types: see getLogger
    _CLASS_HOOKS = {}

    def _log(self, level, msg, args, **kwargs):
        if "timeout" in kwargs:
            timeout = kwargs.pop("timeout")
            epoch = time()
            trap_key = (msg, args)
            if trap_key in _Logger._LOGGER_TIMEOUTS:
                if (epoch - _Logger._LOGGER_TIMEOUTS[trap_key]) < timeout:
                    if self.isEnabledFor(mlc.CONF_LOGGING_VERBOSE):
                        super()._log(
                            mlc.CONF_LOGGING_VERBOSE,
                            f"dropped log message for {msg}",
                            args,
                            **kwargs,
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch

        super()._log(level, msg, args, **kwargs)


LOGGER = getLogger(__name__[:-8])  # get base custom_component name for logging
"""Root meross_lan logger"""


class Loggable(abc.ABC):
    """
    Helper base class for logging instance name/id related info.
    Derived classes can customize this in different flavours:
    - basic way is to override 'logtag' to provide a custom name when
    logging.
    - custom way by overriding 'log' like in 'MerossDevice' we can
    intercept log messages.
    """

    VERBOSE = mlc.CONF_LOGGING_VERBOSE
    DEBUG = mlc.CONF_LOGGING_DEBUG
    INFO = mlc.CONF_LOGGING_INFO
    WARNING = mlc.CONF_LOGGING_WARNING
    CRITICAL = mlc.CONF_LOGGING_CRITICAL

    # hass, api: set when initializing MerossApi
    hass: typing.ClassVar[HomeAssistant] = None  # type: ignore
    """Cached HomeAssistant instance (Boom!)"""
    api: typing.ClassVar[MerossApi] = None  # type: ignore
    """Cached MerossApi instance (Boom!)"""

    @staticmethod
    def get_device_registry():
        return device_registry.async_get(Loggable.hass)

    @staticmethod
    def get_entity_registry():
        return entity_registry.async_get(Loggable.hass)

    __slots__ = (
        "id",
        "logtag",
        "logger",
    )

    def __init__(
        self,
        id,
        *,
        logger: Loggable | logging.Logger = LOGGER,
    ):
        self.id: Final = id
        self.logger = logger
        self.configure_logger()
        self.log(self.DEBUG, "init")

    def configure_logger(self):
        self.logtag = f"{self.__class__.__name__}({self.id})"

    def isEnabledFor(self, level: int):
        return self.logger.isEnabledFor(level)

    def log(self, level: int, msg: str, *args, **kwargs):
        self.logger.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def log_exception(
        self, level: int, exception: Exception, msg: str, *args, **kwargs
    ):
        self.log(
            level,
            f"{exception.__class__.__name__}({str(exception)}) in {msg}",
            *args,
            **kwargs,
        )

    @contextmanager
    def exception_warning(self, msg: str, *args, **kwargs):
        try:
            yield
        except Exception as exception:
            self.log(
                self.WARNING,
                f"{exception.__class__.__name__}({str(exception)}) in {msg}",
                *args,
                **kwargs,
            )

    def __del__(self):
        self.log(self.DEBUG, "destroy")
