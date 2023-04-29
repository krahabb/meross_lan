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

from .const import CONF_CLOUD_KEY, CONF_DEVICE_ID, CONF_HOST, DOMAIN
from .merossclient import const as mc

try:
    from homeassistant.backports.enum import (
        StrEnum,  # type: ignore pylint: disable=unused-import
    )
except Exception:
    import enum

    class StrEnum(enum.Enum):
        """
        convenience alias for homeassistant.backports.StrEnum
        """

        def __str__(self):
            return str(self.value)


if typing.TYPE_CHECKING:
    from typing import Callable, ClassVar, Coroutine

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State

    from . import MerossApi
    from .meross_device import MerossDevice
    from .meross_profile import MerossCloudProfile


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
    Obfuscation:

    There are 2 different approaches both producing the same result by working
    on a set of well-known keys to hide values from a structure. The 'OBFUSCATE_KEYS'
    dict mandates which key values are patched:

    - Obfuscate values 'in place' and then allow to deobfuscate. This approach
    is less memory intensive since it will not duplicate data but will just
    'patch' the passed in structure. 'deobfuscate' will then be able to restore
    the structure to the original values in case. 'obfuscate' will modify the
    passed in data (which must be mutable) only where obfuscation occurs

    - Deepcopy the data. This is useful when source data are immutable and/or
    there's the need to deepcopy the data anyway
"""
# common (shared) obfuscation mappings for related keys
OBFUSCATE_DEVICE_ID_MAP = {}
OBFUSCATE_HOST_MAP = {}
OBFUSCATE_USERID_MAP = {}
OBFUSCATE_SERVER_MAP = {}
OBFUSCATE_PORT_MAP = {}
OBFUSCATE_KEY_MAP = {}
OBFUSCATE_KEYS = {
    # MEROSS PROTOCOL PAYLOADS keys
    # devices uuid(s) is better obscured since knowing this
    # could allow malicious attempts at the public Meross mqtt to
    # correctly address the device (with some easy hacks on signing)
    mc.KEY_UUID: OBFUSCATE_DEVICE_ID_MAP,
    mc.KEY_MACADDRESS: {},
    mc.KEY_WIFIMAC: {},
    mc.KEY_INNERIP: OBFUSCATE_HOST_MAP,
    mc.KEY_SERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_PORT: OBFUSCATE_PORT_MAP,
    mc.KEY_SECONDSERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_SECONDPORT: OBFUSCATE_PORT_MAP,
    mc.KEY_USERID: OBFUSCATE_USERID_MAP,
    mc.KEY_TOKEN: {},
    mc.KEY_KEY: OBFUSCATE_KEY_MAP,
    #
    # MEROSS CLOUD HTTP API KEYS
    mc.KEY_USERID_: OBFUSCATE_USERID_MAP,
    mc.KEY_EMAIL: {},
    # mc.KEY_KEY: OBFUSCATE_KEY_MAP,
    # mc.KEY_TOKEN: {},
    mc.KEY_CLUSTER: {},
    mc.KEY_DOMAIN: OBFUSCATE_SERVER_MAP,
    mc.KEY_RESERVEDDOMAIN: OBFUSCATE_SERVER_MAP,
    # subdevice(s) ids are hardly sensitive since they
    # cannot be accessed over the api without knowing the uuid
    # of the hub device (which is obfuscated indeed). Masking
    # this would also require to obfuscate mc.KEY_ID used by hubs
    # and dumped in traces
    # mc.KEY_SUBDEVICEID: {},
    #
    # ConfigEntries keys
    CONF_DEVICE_ID: OBFUSCATE_DEVICE_ID_MAP,
    CONF_HOST: OBFUSCATE_HOST_MAP,
    # CONF_KEY: OBFUSCATE_KEY_MAP,
    CONF_CLOUD_KEY: OBFUSCATE_KEY_MAP,
    #
    # MerossCloudProfile keys
    "appId": {},
}


def _obfuscated_value(obfuscated_map: dict[typing.Any, str], value: typing.Any):
    """
    for every value we obfuscate, we'll keep
    a cache of 'unique' obfuscated values in order
    to be able to relate 'stable' identical vales in traces
    for debugging/diagnostics purposes
    """
    if obfuscated_map is OBFUSCATE_USERID_MAP:
        # terrible patch here since we want to match
        # values (userid) which are carried both as strings
        # (in mc.KEY_USERID_) and as int (in mc.KEY_USERID)
        try:
            # no type checks before conversion since we're
            # confident its almost an integer decimal number
            value = int(value)
        except Exception:
            # but we play safe anyway
            pass
    elif obfuscated_map is OBFUSCATE_SERVER_MAP:
        # mc.KEY_DOMAIN and mc.KEY_RESERVEDDOMAIN could
        # carry the protocol port embedded like: "server.domain.com:port"
        # so, in order to map to the same values as in mc.KEY_SERVER,
        # mc.KEY_PORT and the likes we'll need special processing
        try:
            if (colon_index := value.find(":")) != -1:
                host = value[0:colon_index]
                port = int(value[colon_index + 1 :])
                return ":".join(
                    (
                        _obfuscated_value(OBFUSCATE_SERVER_MAP, host),
                        _obfuscated_value(OBFUSCATE_PORT_MAP, port),
                    )
                )
        except Exception:
            pass

    if value not in obfuscated_map:
        # first time seen: generate the obfuscation
        count = len(obfuscated_map)
        if isinstance(value, str):
            # we'll preserve string length when obfuscating strings
            obfuscated_value = str(count)
            padding = len(value) - len(obfuscated_value)
            if padding > 0:
                obfuscated_map[value] = "#" * padding + obfuscated_value
            else:
                obfuscated_map[value] = "#" + obfuscated_value
        else:
            obfuscated_map[value] = "@" + str(count)

    return obfuscated_map[value]


def obfuscate(payload: dict):
    """
    parses the input payload and 'hides' (obfuscates) some sensitive keys.
    Obfuscation keeps a static list of obfuscated values (in OBFUSCATE_KEYS)
    so to always obfuscate an input value to the same stable value.
    This function is recursive

    - payload(input-output): gets modified by obfuscating sensistive keys

    - return: a dict of the original values which were obfuscated
    (to be used in 'deobfuscate')
    """
    obfuscated = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            o = obfuscate(value)
            if o:
                obfuscated[key] = o
        elif key in OBFUSCATE_KEYS:
            # save for deobfuscate handling
            obfuscated[key] = value
            payload[key] = _obfuscated_value(OBFUSCATE_KEYS[key], value)

    return obfuscated


def deobfuscate(payload: dict, obfuscated: dict):
    for key, value in obfuscated.items():
        if isinstance(value, dict):
            deobfuscate(payload[key], value)
        else:
            payload[key] = value


def obfuscated_list_copy(data: typing.Sequence):
    return [
        obfuscated_dict_copy(value)  # type: ignore
        if isinstance(value, dict)
        else obfuscated_list_copy(value)
        if isinstance(value, list)
        else value
        for value in data
    ]


def obfuscated_dict_copy(data: dict):
    return {
        key: obfuscated_dict_copy(value)
        if isinstance(value, dict)
        else obfuscated_list_copy(value)
        if isinstance(value, list)
        else _obfuscated_value(OBFUSCATE_KEYS[key], value)
        if key in OBFUSCATE_KEYS
        else value
        for key, value in data.items()
    }


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
    # profiles: list of known cloud profiles (same as devices)
    profiles: ClassVar[dict[str, MerossCloudProfile | None]] = {}

    @staticmethod
    def active_devices():
        return (device for device in ApiProfile.devices.values() if device is not None)

    @staticmethod
    def active_profiles():
        return (
            profile for profile in ApiProfile.profiles.values() if profile is not None
        )

    @staticmethod
    def get_device_with_mac(macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in ApiProfile.active_devices():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    # instance attributes
    async def async_shutdown(self):
        self.unlisten_entry_update()

    @property
    @abc.abstractmethod
    def key(self) -> str | None:
        return NotImplemented

    unsub_entry_update_listener: Callable | None = None

    def listen_entry_update(self, config_entry: ConfigEntry):
        self.unsub_entry_update_listener = config_entry.add_update_listener(
            self.entry_update_listener
        )

    def unlisten_entry_update(self):
        if self.unsub_entry_update_listener is not None:
            self.unsub_entry_update_listener()
            self.unsub_entry_update_listener = None

    @abc.abstractmethod
    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        raise NotImplementedError()
