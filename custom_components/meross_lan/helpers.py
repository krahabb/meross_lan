"""
    Helpers!
"""
from __future__ import annotations

import abc
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import logging
from logging import DEBUG, WARNING
from time import gmtime, time
import typing

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.util.dt import DEFAULT_TIME_ZONE, utcnow

from .const import (
    CONF_ALLOW_MQTT_PUBLISH,
    CONF_CLOUD_KEY,
    CONF_CREATE_DIAGNOSTIC_ENTITIES,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    DOMAIN,
)
from .merossclient import const as mc, get_default_arguments

try:
    # since we're likely on python3.11 this should quickly
    # set our StrEnum symbol
    from enum import StrEnum  # type: ignore pylint: disable=unused-import
except Exception:
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
    from datetime import tzinfo
    from typing import Callable, ClassVar, Coroutine, Final

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State

    from . import MerossApi
    from .meross_device import MerossDevice
    from .meross_entity import MerossEntity
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
    return utcdt if tz is timezone.utc else utcdt.astimezone(tz or DEFAULT_TIME_ZONE)


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
                    if self.isEnabledFor(DEBUG):
                        super()._log(
                            DEBUG,
                            f"dropped log message for {msg}",
                            args,
                            **kwargs,
                        )
                    return
            _Logger._LOGGER_TIMEOUTS[trap_key] = epoch

        super()._log(level, msg, args, **kwargs)


LOGGER = getLogger(__name__[:-8])  # get base custom_component name for logging


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
    mc.KEY_SSID: {},
    mc.KEY_GATEWAYMAC: {},
    mc.KEY_INNERIP: OBFUSCATE_HOST_MAP,
    mc.KEY_SERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_PORT: OBFUSCATE_PORT_MAP,
    mc.KEY_SECONDSERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_SECONDPORT: OBFUSCATE_PORT_MAP,
    mc.KEY_ACTIVESERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_MAINSERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_MAINPORT: OBFUSCATE_PORT_MAP,
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


def obfuscated_list_copy(data: list):
    return [
        obfuscated_dict_copy(value)
        if isinstance(value, dict)
        else obfuscated_list_copy(value)
        if isinstance(value, list)
        else value
        for value in data
    ]


def obfuscated_dict_copy(data: typing.Mapping[str, typing.Any]):
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
    if states := await get_entity_last_states(hass, 2, entity_id):
        for state in reversed(states):
            if state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                return state
    return None


class PollingStrategy:
    """
    These helper class(es) is used to implement 'smart' polling
    bases on current state of device, especially regarding MQTT availability.
    In fact, on MQTT we can receive almost all of the state through async PUSHES
    and we so avoid any polling. This is not true for everything (for example it looks
    in general that configurations are not pushed though). We use the namespace
    to decide which policy is best for.
    See __call__ implementation(s) for the different behaviors
    """

    __slots__ = (
        "namespace",
        "request",
        "lastrequest",
    )

    def __init__(self, namespace: str, payload: dict | None = None):
        self.namespace = namespace
        self.request = (
            get_default_arguments(namespace)
            if payload is None
            else (
                namespace,
                mc.METHOD_GET,
                payload,
            )
        )
        self.lastrequest = 0

    async def poll(self, device: MerossDevice, epoch: float, namespace: str | None):
        """
        This is a basic 'default' policy:
        - avoid the request when MQTT available (this is for general 'state' namespaces like NS_ALL) and
        we expect this namespace to be updated by PUSH(es)
        - unless the passed in 'namespace' is not None which means we're re-onlining the device and so
        we like to re-query the full state (even on MQTT)
        - as an optimization, when onlining (namespace == None), we'll skip the request if it's for
        the same namespace by not calling this strategy (see MerossDevice.async_request_updates)
        """
        if namespace or (not device._mqtt_active):
            await device.async_request(*self.request)
            self.lastrequest = epoch


class SmartPollingStrategy(PollingStrategy):
    """
    This is a strategy for polling states which are not actively pushed so we should
    always query them (eventually with a variable timeout depending on the relevant
    time dynamics of the sensor/state). When using cloud MQTT though we have to be very
    conservative on traffic so we delay the request even more
    """

    __slots__ = ("polling_period",)

    def __init__(
        self, namespace: str, payload: dict | None = None, polling_period: int = 0
    ):
        super().__init__(namespace, payload)
        self.polling_period = polling_period

    async def poll(self, device: MerossDevice, epoch: float, namespace: str | None):
        if (epoch - self.lastrequest) >= self.polling_period:
            if await device.async_request_smartpoll(
                epoch,
                self.lastrequest,
                self.request,
            ):
                self.lastrequest = epoch


class EntityPollingStrategy(SmartPollingStrategy):
    __slots__ = ("entity",)

    def __init__(self, namespace: str, entity: MerossEntity, polling_period: int = 0):
        super().__init__(namespace, None, polling_period)
        self.entity = entity

    async def poll(self, device: MerossDevice, epoch: float, namespace: str | None):
        """
        Same as SmartPollingStrategy but we have a 'relevant' entity associated with
        the state of this paylod so we'll skip the smartpoll should the entity be disabled
        """
        if self.entity.enabled:
            await super().poll(device, epoch, namespace)


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


class Loggable(abc.ABC):
    """
    Helper base class for logging instance name/id related info.
    Derived classes can customize this in different flavours:
    - basic way is to override 'logtag' to provide a custom name when
    logging.
    - custom way by overriding 'log' like in 'MerossDevice' we can
    intercept log messages.
    """

    id: Final

    __slots__ = (
        "id",
        "logtag",
        "logger",
    )

    def __init__(self, id, logtag: str | None = None, logger: Loggable | logging.Logger = LOGGER):
        self.id = id
        self.logtag = logtag or f"{self.__class__.__name__}({id})"
        self.logger = logger
        logger.debug("%s: init", self.logtag)

    def isEnabledFor(self, level: int):
        return self.logger.isEnabledFor(level)

    def log(self, level: int, msg: str, *args, **kwargs):
        self.logger.log(level, f"{self.logtag}: {msg}", *args, **kwargs)

    def warning_enabled(self):
        return self.logger.isEnabledFor(WARNING)

    def warning(self, msg: str, *args, **kwargs):
        self.log(WARNING, msg, *args, **kwargs)

    def debug_enabled(self):
        return self.logger.isEnabledFor(DEBUG)

    def debug(self, msg: str, *args, **kwargs):
        self.log(DEBUG, msg, *args, **kwargs)

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
        self.log(
            WARNING,
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
                WARNING,
                f"{exception.__class__.__name__}({str(exception)}) in {msg}",
                *args,
                **kwargs,
            )

    def __del__(self):
        self.logger.debug("%s: destroy", self.logtag)
        return


class EntityManager(Loggable):
    """
    This is an abstraction of an actual (device or other) container
    for MerossEntity(s). This container is very 'hybrid', end its main purpose
    is to provide interfaces to their owned MerossEntities.
    It could represent a MerossDevice, a MerossSubDevice or an ApiProfile
    and groups entities associated with a ConfigEntry
    """

    config_entry_id: Final[str]
    deviceentry_id: dict[str, set] | None
    key: str

    __slots__ = (
        "config_entry_id",
        "deviceentry_id",
        "entities",
        "platforms",
        "key",
        "config",
        "_unsub_entry_update_listener",
        "_unsub_entry_reload_scheduler",
    )

    def __init__(
        self,
        id: str,
        config_entry_or_id: ConfigEntry | str = "",
        deviceentry_id: dict[str, set] | None = None,
    ):
        super().__init__(id)
        self.deviceentry_id = deviceentry_id
        # This is a collection of all of the instanced entities
        # they're generally built here during inherited __init__ and will be registered
        # in platforms(s) async_setup_entry with their corresponding platform
        self.entities: dict[object, "MerossEntity"] = {}
        # when we build an entity we also add the relative platform name here
        # so that the async_setup_entry for this integration will be able to forward
        # the setup to the appropriate platform(s).
        # The item value here will be set to the async_add_entities callback
        # during the corresponding platform async_setup_entry so to be able
        # to dynamically add more entities should they 'pop-up' (Hub only?)
        self.platforms: dict[str, Callable | None] = {}

        if isinstance(config_entry_or_id, str):
            self.config_entry_id = config_entry_or_id
            self.key = ""
            self.config = {}
        else:
            self.config_entry_id = config_entry_or_id.entry_id
            self.config = config_entry_or_id.data
            self.key = config_entry_or_id.data.get(CONF_KEY) or ""
        self._unsub_entry_update_listener = None
        self._unsub_entry_reload_scheduler: asyncio.TimerHandle | None = None

    @property
    def name(self) -> str:
        return self.logtag

    @property
    def online(self):
        return True

    async def async_shutdown(self):
        """
        Cleanup code called when the config entry is unloaded.
        Beware, when a derived class owns some direct member pointers to entities,
        be sure to invalidate them after calling the super() implementation.
        This is especially true for MerossDevice(s) classes which need to stop
        their async polling before invalidating the member pointers (which are
        usually referred to inside the polling /parsing code)
        """
        self.unlisten_entry_update()  # extra-safety cleanup: shouldnt be loaded/listened at this point
        self.unschedule_entry_reload()
        for entity in self.entities.values():
            await entity.async_shutdown()
        self.entities.clear()

    async def async_setup_entry(self, hass: HomeAssistant, config_entry: ConfigEntry):
        assert not self._unsub_entry_update_listener
        assert config_entry.entry_id not in ApiProfile.managers
        self.config_entry_id = config_entry.entry_id  # type: ignore
        self.config = config_entry.data
        ApiProfile.managers[self.config_entry_id] = self
        await hass.config_entries.async_forward_entry_setups(
            config_entry, self.platforms.keys()
        )
        self._unsub_entry_update_listener = config_entry.add_update_listener(
            self.entry_update_listener
        )

    async def async_unload_entry(self, hass: HomeAssistant, config_entry: ConfigEntry):
        if not await hass.config_entries.async_unload_platforms(
            config_entry, self.platforms.keys()
        ):
            return False
        self.unlisten_entry_update()
        self.unschedule_entry_reload()
        ApiProfile.managers.pop(self.config_entry_id)
        return True

    def unlisten_entry_update(self):
        if self._unsub_entry_update_listener:
            self._unsub_entry_update_listener()
            self._unsub_entry_update_listener = None

    def schedule_entry_reload(self):
        """Schedules a reload (in 15 sec) of the config_entry performing a full re-initialization"""
        self.unschedule_entry_reload()

        async def _async_entry_reload():
            self._unsub_entry_reload_scheduler = None
            await ApiProfile.hass.config_entries.async_reload(self.config_entry_id)

        self._unsub_entry_reload_scheduler = schedule_async_callback(
            ApiProfile.hass, 15, _async_entry_reload
        )

    def unschedule_entry_reload(self):
        if self._unsub_entry_reload_scheduler:
            self._unsub_entry_reload_scheduler.cancel()
            self._unsub_entry_reload_scheduler = None

    def managed_entities(self, platform):
        """entities list for platform setup"""
        return [
            entity for entity in self.entities.values() if entity.PLATFORM is platform
        ]

    def generate_unique_id(self, entity: MerossEntity):
        """
        flexible policy in order to generate unique_ids for entities:
        This is an helper needed to better control migrations in code
        which could/would lead to a unique_id change.
        We could put here code checks in order to avoid entity_registry
        migrations
        """
        return f"{self.id}_{entity.id}"

    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        self.config = config_entry.data
        self.key = config_entry.data.get(CONF_KEY) or ""


class ApiProfile(EntityManager):
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
    # managers: represents the container of entities belonging to a ConfigEntry
    managers: ClassVar[dict[str, EntityManager]] = {}

    @staticmethod
    def active_devices():
        return (device for device in ApiProfile.devices.values() if device)

    @staticmethod
    def active_profiles():
        return (profile for profile in ApiProfile.profiles.values() if profile)

    @staticmethod
    def get_device_with_mac(macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in ApiProfile.active_devices():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    @property
    def allow_mqtt_publish(self):
        return self.config.get(CONF_ALLOW_MQTT_PUBLISH)

    @property
    def create_diagnostic_entities(self):
        return self.config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES)

    @abc.abstractmethod
    def attach_mqtt(self, device: MerossDevice):
        pass
