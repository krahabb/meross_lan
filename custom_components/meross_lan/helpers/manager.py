from __future__ import annotations

import abc
import asyncio
from enum import StrEnum
import logging
import os
from time import localtime, strftime, time
import typing

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import LOGGER, Loggable, getLogger, schedule_async_callback, schedule_callback
from ..const import (
    CONF_ALLOW_MQTT_PUBLISH,
    CONF_CREATE_DIAGNOSTIC_ENTITIES,
    CONF_KEY,
    CONF_LOGGING_LEVEL,
    CONF_LOGGING_LEVEL_OPTIONS,
    CONF_OBFUSCATE,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_MQTT,
    CONF_TRACE,
    CONF_TRACE_DIRECTORY,
    CONF_TRACE_FILENAME,
    CONF_TRACE_MAXSIZE,
    CONF_TRACE_TIMEOUT,
    CONF_TRACE_TIMEOUT_DEFAULT,
    DOMAIN,
)
from ..merossclient import cloudapi, const as mc, json_dumps
from .obfuscate import (
    OBFUSCATE_DEVICE_ID_MAP,
    OBFUSCATE_SERVER_MAP,
    OBFUSCATE_USERID_MAP,
    obfuscated_any,
    obfuscated_dict,
)

if typing.TYPE_CHECKING:
    from io import TextIOWrapper
    from typing import Callable, ClassVar, Final

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..meross_device import MerossDevice
    from ..meross_entity import MerossEntity
    from ..meross_profile import MerossCloudProfile, MQTTConnection
    from ..merossclient import HostAddress, MerossMessage, MerossPayloadType


class ManagerState(StrEnum):
    INIT = "init"
    LOADING = "loading"
    LOADED = "loaded"
    STARTED = "started"
    SHUTDOWN = "shutdown"


class EntityManager(Loggable):
    """
    This is an abstraction of an actual (device or other) container
    for MerossEntity(s). This container is very 'hybrid', end its main purpose
    is to provide interfaces to their owned MerossEntities.
    It could represent a MerossDevice, a MerossSubDevice or an ApiProfile
    and manages the relation(s) with the ConfigEntry (config, life-cycle).
    This is a 'partial' base class for ConfigEntryManager which definitely establishes
    the relationship with the ConfigEntry. This is in turn needed to better establish
    an isolation level between MerossSubDevice and a ConfigEntry
    """

    # slots for ConfigEntryManager are defined here since we would have some
    # multiple inheritance conflicts in MerossDevice
    __slots__ = (
        "config_entry_id",
        "deviceentry_id",
        "entities",
        "platforms",
        "config",
        "key",
        "obfuscate",
        "state",
        "_trace_file",
        "_trace_future",
        "_trace_data",
        "_unsub_trace_endtime",
        "_unsub_entry_update_listener",
        "_unsub_entry_reload_scheduler",
    )

    def __init__(
        self,
        id: str,
        *,
        config_entry_id: str,
        deviceentry_id: dict[str, set[tuple[str, str]]] | None = None,
        **kwargs,
    ):
        self.config_entry_id = config_entry_id
        self.deviceentry_id = deviceentry_id
        # This is a collection of all of the instanced entities
        # they're generally built here during inherited __init__ and will be registered
        # in platforms(s) async_setup_entry with their corresponding platform
        self.entities: Final[dict[object, MerossEntity]] = {}
        self.state = ManagerState.INIT
        super().__init__(id, **kwargs)

    async def async_shutdown(self):
        """
        Cleanup code called when the config entry is unloaded.
        Beware, when a derived class owns some direct member pointers to entities,
        be sure to invalidate them after calling the super() implementation.
        This is especially true for MerossDevice(s) classes which need to stop
        their async polling before invalidating the member pointers (which are
        usually referred to inside the polling /parsing code)
        """
        for entity in set(self.entities.values()):
            # async_shutdown will pop out of self.entities
            await entity.async_shutdown()

    @property
    def name(self) -> str:
        return self.logtag

    @property
    def online(self):
        return True

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


class ConfigEntryManager(EntityManager):
    """
    This is an abstraction of an actual (device or other) container
    for MerossEntity(s). This container is very 'hybrid', end its main purpose
    is to provide interfaces to their owned MerossEntities.
    It could represent a MerossDevice, a MerossSubDevice or an ApiProfile
    and manages the relation(s) with the ConfigEntry (config, life-cycle)
    """

    TRACE_RX = "RX"
    TRACE_TX = "TX"

    DEFAULT_PLATFORMS: typing.ClassVar[dict[str, Callable | None]] = {}
    """Defined at the class level to preset a list of domains for entities
    which could be dynamically added after ConfigEntry loading."""

    key: str
    logger: logging.Logger

    def __init__(
        self,
        id: str,
        config_entry: ConfigEntry | None,
        **kwargs,
    ):
        if config_entry:
            config_entry_id = config_entry.entry_id
            self.config = config = config_entry.data
            self.key = config.get(CONF_KEY) or ""
            self.obfuscate = config.get(CONF_OBFUSCATE, True)
        else:
            # this is the MerossApi: it will be better initialized when
            # the ConfigEntry is loaded
            config_entry_id = ""
            self.config = {}
            self.key = ""
            self.obfuscate = True
        # when we build an entity we also add the relative platform name here
        # so that the async_setup_entry for this integration will be able to forward
        # the setup to the appropriate platform(s).
        # The item value here will be set to the async_add_entities callback
        # during the corresponding platform async_setup_entry so to be able
        # to dynamically add more entities should they 'pop-up' (Hub only?)
        self.platforms = self.DEFAULT_PLATFORMS.copy()
        self._trace_file: TextIOWrapper | None = None
        self._trace_future: asyncio.Future | None = None
        self._trace_data: list | None = None
        self._unsub_trace_endtime: asyncio.TimerHandle | None = None
        self._unsub_entry_update_listener = None
        self._unsub_entry_reload_scheduler: asyncio.TimerHandle | None = None
        super().__init__(id, config_entry_id=config_entry_id, **kwargs)

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
        await super().async_shutdown()
        await self.async_destroy_diagnostic_entities()
        if self.is_tracing:
            self.trace_close()

    # interface: Loggable
    def configure_logger(self):
        """
        Configure a 'logger' and a 'logtag' based off current config for every ConfigEntry.
        We'll need this updated when CONF_OBFUSCATE changes since
        the name might depend on it. We're then using this call during
        __init__ for the first setup and subsequently when ConfigEntry changes
        """
        self.logtag = self.get_logger_name()
        self.logger = logger = getLogger(f"{LOGGER.name}.{self.logtag}")
        try:
            logger.setLevel(self.config.get(CONF_LOGGING_LEVEL, logging.NOTSET))
        except Exception as exception:
            # do not use self Loggable interface since we might be not set yet
            LOGGER.warning(
                "error (%s) setting log level: likely a corrupted configuration entry",
                str(exception),
            )

    def log(self, level: int, msg: str, *args, **kwargs):
        if (logger := self.logger).isEnabledFor(level):
            logger._log(level, msg, args, **kwargs)
        if self.is_tracing:
            self.trace_log(
                level,
                msg % args,
            )

    # interface: self
    @property
    def create_diagnostic_entities(self):
        return self.config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES)

    async def async_setup_entry(self, hass: HomeAssistant, config_entry: ConfigEntry):
        assert self.state is ManagerState.INIT
        assert config_entry.entry_id not in ApiProfile.managers
        assert self.config_entry_id == config_entry.entry_id
        ApiProfile.managers[self.config_entry_id] = self
        self.state = ManagerState.LOADING
        # open the trace before adding the entities
        # so we could catch logs in this phase too
        state = ApiProfile.managers_transient_state.setdefault(self.config_entry_id, {})
        if state.pop(CONF_TRACE, None):
            self.trace_open()

        if self.config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES):
            await self.async_create_diagnostic_entities()

        await hass.config_entries.async_forward_entry_setups(
            config_entry, self.platforms.keys()
        )
        self._unsub_entry_update_listener = config_entry.add_update_listener(
            self.entry_update_listener
        )
        self.state = ManagerState.LOADED

    async def async_unload_entry(self, hass: HomeAssistant, config_entry: ConfigEntry):
        if not await hass.config_entries.async_unload_platforms(
            config_entry, self.platforms.keys()
        ):
            return False
        self.unlisten_entry_update()
        self.unschedule_entry_reload()
        ApiProfile.managers.pop(self.config_entry_id)
        self.platforms = {}
        self.config = {}
        await self.async_shutdown()
        self.state = ManagerState.INIT
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
            await self.hass.config_entries.async_reload(self.config_entry_id)

        self._unsub_entry_reload_scheduler = schedule_async_callback(
            self.hass, 15, _async_entry_reload
        )

    def unschedule_entry_reload(self):
        if self._unsub_entry_reload_scheduler:
            self._unsub_entry_reload_scheduler.cancel()
            self._unsub_entry_reload_scheduler = None

    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        config = self.config = config_entry.data
        self.key = config.get(CONF_KEY) or ""
        self.obfuscate = config.get(CONF_OBFUSCATE, True)
        self.configure_logger()
        if config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES):
            await self.async_create_diagnostic_entities()
        else:
            await self.async_destroy_diagnostic_entities(True)

    async def async_create_diagnostic_entities(self):
        """Dynamically create some diagnostic entities depending on configuration"""
        pass

    async def async_destroy_diagnostic_entities(self, remove: bool = False):
        """Cleanup diagnostic entities, when the entry is unloaded. If 'remove' is True
        it will be removed from the entity registry as well."""
        ent_reg = self.get_entity_registry() if remove else None
        for entity in self.managed_entities(SENSOR_DOMAIN):
            if entity.is_diagnostic:
                if entity._hass_connected:
                    await entity.async_remove()
                await entity.async_shutdown()
                if ent_reg:
                    ent_reg.async_remove(entity.entity_id)

    @abc.abstractmethod
    def get_logger_name(self) -> str:
        raise NotImplementedError()

    def loggable_any(self, value):
        """
        Conditionally obfuscate any type to send to logging/tracing.
        use the typed versions to increase efficiency/context
        """
        return obfuscated_any(value) if self.obfuscate else value

    def loggable_dict(self, value: typing.Mapping[str, typing.Any]):
        """Conditionally obfuscate the dict values (based off OBFUSCATE_KEYS) to send to logging/tracing"""
        return obfuscated_dict(value) if self.obfuscate else value

    def loggable_broker(self, broker: HostAddress | str):
        """Conditionally obfuscate the connection_id (which is a broker address host:port) to send to logging/tracing"""
        return (
            OBFUSCATE_SERVER_MAP.obfuscate(str(broker))
            if self.obfuscate
            else str(broker)
        )

    def loggable_device_id(self, device_id: str):
        """Conditionally obfuscate the device_id to send to logging/tracing"""
        return (
            OBFUSCATE_DEVICE_ID_MAP.obfuscate(device_id)
            if self.obfuscate
            else device_id
        )

    def loggable_profile_id(self, profile_id: str):
        """Conditionally obfuscate the profile_id (which is the Meross account userId) to send to logging/tracing"""
        return (
            OBFUSCATE_USERID_MAP.obfuscate(profile_id) if self.obfuscate else profile_id
        )

    @property
    def is_tracing(self):
        return self._trace_file or self._trace_data

    def trace_open(self):
        try:
            self.log(self.DEBUG, "Tracing start")
            epoch = time()
            tracedir = self.hass.config.path(
                "custom_components", DOMAIN, CONF_TRACE_DIRECTORY
            )
            os.makedirs(tracedir, exist_ok=True)
            self._trace_file = open(
                os.path.join(
                    tracedir,
                    CONF_TRACE_FILENAME.format(
                        strftime("%Y-%m-%d_%H-%M-%S", localtime(epoch)),
                        self.config_entry_id,
                    ),
                ),
                mode="w",
                encoding="utf8",
            )
            trace_timeout = (
                self.config.get(CONF_TRACE_TIMEOUT) or CONF_TRACE_TIMEOUT_DEFAULT
            )

            @callback
            def _trace_close_callback():
                self._unsub_trace_endtime = None
                self.trace_close()

            self._unsub_trace_endtime = schedule_callback(
                self.hass, trace_timeout, _trace_close_callback
            )
            self._trace_opened(epoch)
        except Exception as exception:
            self.trace_close()
            self.log_exception(self.WARNING, exception, "creating trace file")

    def _trace_opened(self, epoch: float):
        """
        Virtual placeholder called when a new trace is opened.
        Allows derived EntityManagers to log some preamble in the trace.
        """
        pass

    def trace_close(self):
        if self._trace_file:
            try:
                self._trace_file.close()  # type: ignore
            except Exception as exception:
                self.log_exception(self.WARNING, exception, "closing trace file")
            self._trace_file = None
            self.log(self.DEBUG, "Tracing end")
        if self._unsub_trace_endtime:
            self._unsub_trace_endtime.cancel()
            self._unsub_trace_endtime = None
        if self._trace_future:
            self._trace_future.set_result(self._trace_data)
            self._trace_future = None
        self._trace_data = None

    def trace(
        self,
        epoch: float,
        payload: MerossPayloadType,
        namespace: str,
        method: str = mc.METHOD_GETACK,
        protocol: str = CONF_PROTOCOL_AUTO,
        rxtx: str = "",
    ):
        try:
            data = self.loggable_dict(payload)
            columns = [
                strftime("%Y/%m/%d - %H:%M:%S", localtime(epoch)),
                rxtx,
                protocol,
                method,
                namespace,
                data,
            ]
            if self._trace_data:
                self._trace_data.append(columns)
            if self._trace_file:
                columns[5] = json_dumps(data)
                self._trace_file.write("\t".join(columns) + "\r\n")
                columns[5] = data  # restore the (eventual) _trace_data ref
                if self._trace_file.tell() > CONF_TRACE_MAXSIZE:
                    self.trace_close()

        except Exception as exception:
            self.trace_close()
            self.log_exception(self.WARNING, exception, "appending trace data")

    def trace_log(
        self,
        level: int,
        msg: str,
    ):
        try:
            columns = [
                strftime("%Y/%m/%d - %H:%M:%S", localtime(time())),
                "",
                CONF_PROTOCOL_AUTO,
                "LOG",
                CONF_LOGGING_LEVEL_OPTIONS.get(level) or logging.getLevelName(level),
                msg,
            ]
            if self._trace_data:
                self._trace_data.append(columns)
            if self._trace_file:
                self._trace_file.write("\t".join(columns) + "\r\n")
                if self._trace_file.tell() > CONF_TRACE_MAXSIZE:
                    self.trace_close()

        except Exception as exception:
            self.trace_close()
            self.log_exception(self.WARNING, exception, "appending trace log")


class ApiProfile(ConfigEntryManager):
    """
    Base class for both MerossCloudProfile and MerossApi allowing lightweight
    sharing of globals and defining some common interfaces.
    """

    DEFAULT_PLATFORMS = ConfigEntryManager.DEFAULT_PLATFORMS | {
        SENSOR_DOMAIN: None,
    }

    devices: ClassVar[dict[str, MerossDevice | None]] = {}
    """
    dict of configured devices. Every device config_entry in the system is mapped here and
    set to the MerossDevice instance if the device is actually active (config_entry loaded)
    or set to None if the config_entry is not loaded (no device instance)
    """
    profiles: ClassVar[dict[str, MerossCloudProfile | None]] = {}
    """
    dict of configured cloud profiles (behaves as the 'devices' dict).
    """
    managers: ClassVar[dict[str, ConfigEntryManager]] = {}
    """
    dict of loaded EntityManagers (ApiProfile(s) or devices) and
    matches exactly the loaded config entries.
    """
    managers_transient_state: ClassVar[dict[str, dict]] = {}
    """
    This is actually a temporary memory storage used to mantain some info related to
    an ConfigEntry/EntityManager that we don't want to persist to hass storage (useless overhead)
    since they're just runtime context but we need an independent storage than
    EntityManager since these info are needed during EntityManager async_setup_entry.
    See the tracing feature activated through the OptionsFlow for insights.
    """

    @staticmethod
    def active_devices():
        """Iterates over the currently loaded MerossDevices."""
        return (device for device in ApiProfile.devices.values() if device)

    @staticmethod
    def active_profiles():
        """Iterates over the currently loaded MerossCloudProfiles."""
        return (profile for profile in ApiProfile.profiles.values() if profile)

    @staticmethod
    def get_device_with_mac(macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in ApiProfile.active_devices():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    __slots__ = (
        "linkeddevices",
        "mqttconnections",
    )

    def __init__(self, id: str, config_entry: ConfigEntry | None):
        super().__init__(id, config_entry)
        self.linkeddevices: dict[str, MerossDevice] = {}
        self.mqttconnections: dict[str, MQTTConnection] = {}

    # interface: ConfigEntryManager
    async def async_shutdown(self):
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_shutdown()
        self.mqttconnections.clear()
        for device in self.linkeddevices.values():
            device.profile_unlinked()
        self.linkeddevices.clear()
        await super().async_shutdown()

    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        config = config_entry.data
        # the MerossApi always enable (independent of config) mqtt publish
        allow_mqtt_publish = config.get(CONF_ALLOW_MQTT_PUBLISH) or (self is self.api)
        if allow_mqtt_publish != self.allow_mqtt_publish:
            # device._mqtt_publish is rather 'passive' so
            # we do some fast 'smart' updates:
            if allow_mqtt_publish:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = device._mqtt_connected
            else:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = None
        await super().entry_update_listener(hass, config_entry)
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.entry_update_listener(self)

    async def async_create_diagnostic_entities(self):
        await super().async_create_diagnostic_entities()
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_create_diagnostic_entities()

    # interface: self
    @property
    def allow_mqtt_publish(self):
        return self.config.get(CONF_ALLOW_MQTT_PUBLISH)

    def try_link(self, device: MerossDevice):
        device_id = device.id
        if device_id not in self.linkeddevices:
            device.profile_linked(self)
            self.linkeddevices[device_id] = device
            return True
        return False

    def unlink(self, device: MerossDevice):
        device_id = device.id
        assert device_id in self.linkeddevices
        device.profile_unlinked()
        self.linkeddevices.pop(device_id)

    @abc.abstractmethod
    def attach_mqtt(self, device: MerossDevice):
        pass

    def trace_or_log(
        self,
        connection: MQTTConnection,
        device_id: str,
        message: MerossMessage,
        rxtx: str,
    ):
        if self.is_tracing:
            header = message[mc.KEY_HEADER]
            self.trace(
                time(),
                message[mc.KEY_PAYLOAD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_METHOD],
                CONF_PROTOCOL_MQTT,
                rxtx,
            )
        elif self.isEnabledFor(self.VERBOSE):
            header = message[mc.KEY_HEADER]
            connection.log(
                self.VERBOSE,
                "%s(%s) %s %s (uuid:%s messageId:%s) %s",
                rxtx,
                CONF_PROTOCOL_MQTT,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                self.loggable_device_id(device_id),
                header[mc.KEY_MESSAGEID],
                json_dumps(obfuscated_dict(message))
                if self.obfuscate
                else message.json(),
            )
        elif self.isEnabledFor(self.DEBUG):
            header = message[mc.KEY_HEADER]
            connection.log(
                self.DEBUG,
                "%s(%s) %s %s (uuid:%s messageId:%s)",
                rxtx,
                CONF_PROTOCOL_MQTT,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                self.loggable_device_id(device_id),
                header[mc.KEY_MESSAGEID],
            )


class CloudApiClient(cloudapi.CloudApiClient, Loggable):
    """
    A specialized cloudapi.CloudApiClient providing meross_lan style logging
    interface to the underlying cloudapi services.
    """

    def __init__(
        self,
        manager: ConfigEntryManager,
        credentials: cloudapi.MerossCloudCredentials | None = None,
    ):
        Loggable.__init__(self, "", logger=manager)
        cloudapi.CloudApiClient.__init__(
            self,
            credentials=credentials,
            session=async_get_clientsession(Loggable.hass),
            logger=self,  # type: ignore (Loggable almost duck-compatible with logging.Logger)
            obfuscate_func=manager.loggable_any,
        )
