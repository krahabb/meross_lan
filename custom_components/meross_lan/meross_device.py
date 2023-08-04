from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from io import TextIOWrapper
from json import dumps as json_dumps
from logging import DEBUG, getLevelName as logging_getLevelName
import os
from time import localtime, strftime, time
import typing
from uuid import uuid4
import weakref
from zoneinfo import ZoneInfo

from homeassistant.core import callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    CONF_POLLING_PERIOD_DEFAULT,
    CONF_POLLING_PERIOD_MIN,
    CONF_PROFILE_ID_LOCAL,
    CONF_PROTOCOL,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    CONF_PROTOCOL_OPTIONS,
    CONF_TIMESTAMP,
    CONF_TRACE,
    CONF_TRACE_DIRECTORY,
    CONF_TRACE_FILENAME,
    CONF_TRACE_MAXSIZE,
    CONF_TRACE_TIMEOUT_DEFAULT,
    DOMAIN,
    PARAM_CLOUDMQTT_UPDATE_PERIOD,
    PARAM_COLDSTARTPOLL_DELAY,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_SIGNAL_UPDATE_PERIOD,
    PARAM_TIMESTAMP_TOLERANCE,
    PARAM_TIMEZONE_CHECK_PERIOD,
    PARAM_TRACING_ABILITY_POLL_TIMEOUT,
    DeviceConfigType,
)
from .helpers import (
    LOGGER,
    ApiProfile,
    EntityManager,
    EntityPollingStrategy,
    PollingStrategy,
    datetime_from_epoch,
    obfuscated_dict_copy,
    schedule_async_callback,
    schedule_callback,
)
from .meross_entity import MerossFakeEntity
from .merossclient import (  # mEROSS cONST
    MEROSSDEBUG,
    const as mc,
    get_default_arguments,
    get_message_signature,
    get_namespacekey,
    is_device_online,
)
from .merossclient.httpclient import MerossHttpClient
from .sensor import PERCENTAGE, MLSensor, ProtocolSensor
from .update import MLUpdate

ResponseCallbackType = typing.Callable[[bool, dict, dict], None]

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_entity import MerossEntity
    from .meross_profile import MerossCloudProfile, MQTTConnection
    from .merossclient import MerossDeviceDescriptor
    from .merossclient.cloudapi import (
        DeviceInfoType,
        LatestVersionType,
        SubDeviceInfoType,
    )

# when tracing we enumerate appliance abilities to get insights on payload structures
# this list will be excluded from enumeration since it's redundant/exposing sensitive info
# or simply crashes/hangs the device
TRACE_ABILITY_EXCLUDE = (
    mc.NS_APPLIANCE_SYSTEM_ALL,
    mc.NS_APPLIANCE_SYSTEM_ABILITY,
    mc.NS_APPLIANCE_SYSTEM_DNDMODE,
    mc.NS_APPLIANCE_SYSTEM_TIME,
    mc.NS_APPLIANCE_SYSTEM_HARDWARE,
    mc.NS_APPLIANCE_SYSTEM_FIRMWARE,
    mc.NS_APPLIANCE_SYSTEM_ONLINE,
    mc.NS_APPLIANCE_SYSTEM_REPORT,
    mc.NS_APPLIANCE_SYSTEM_DEBUG,
    mc.NS_APPLIANCE_SYSTEM_CLOCK,
    mc.NS_APPLIANCE_DIGEST_TRIGGERX,
    mc.NS_APPLIANCE_DIGEST_TIMERX,
    mc.NS_APPLIANCE_CONFIG_KEY,
    mc.NS_APPLIANCE_CONFIG_WIFI,
    mc.NS_APPLIANCE_CONFIG_WIFIX,  # disconnects
    mc.NS_APPLIANCE_CONFIG_WIFILIST,
    mc.NS_APPLIANCE_CONFIG_TRACE,
    mc.NS_APPLIANCE_CONTROL_BIND,
    mc.NS_APPLIANCE_CONTROL_UNBIND,
    mc.NS_APPLIANCE_CONTROL_MULTIPLE,
    mc.NS_APPLIANCE_CONTROL_UPGRADE,  # disconnects
    mc.NS_APPLIANCE_CONTROL_TRIGGERX,
    mc.NS_APPLIANCE_CONTROL_TIMERX,
    mc.NS_APPLIANCE_HUB_EXCEPTION,  # disconnects
    mc.NS_APPLIANCE_HUB_REPORT,  # disconnects
    mc.NS_APPLIANCE_HUB_SUBDEVICELIST,  # disconnects
    mc.NS_APPLIANCE_HUB_PAIRSUBDEV,  # disconnects
    mc.NS_APPLIANCE_MCU_UPGRADE,  # disconnects
    mc.NS_APPLIANCE_MCU_HP110_PREVIEW,  # disconnects
    mc.NS_APPLIANCE_MCU_FIRMWARE,  # disconnects
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK,  # disconnects
)

TRACE_DIRECTION_RX = "RX"
TRACE_DIRECTION_TX = "TX"

TIMEZONES_SET = None


class _MQTTTransaction:
    __slots__ = (
        "namespace",
        "messageid",
        "method",
        "request_time",
        "response_callback",
    )

    def __init__(
        self, namespace: str, method: str, response_callback: ResponseCallbackType
    ):
        self.namespace = namespace
        self.messageid = uuid4().hex
        self.method = method
        self.request_time = time()
        self.response_callback = response_callback


class MerossDeviceBase(EntityManager):
    """
    Abstract base class for MerossDevice and MerossSubDevice (from hub)
    giving common behaviors like device_registry interface
    """

    deviceentry_id: dict[str, set]
    # device info dict from meross cloud api
    device_info: DeviceInfoType | SubDeviceInfoType | None

    __slots__ = (
        "deviceentry_id",
        "device_info",
        "_online",
        "_device_registry_entry",
    )

    def __init__(
        self,
        id_: str,
        config_entry_or_id: ConfigEntry | str,
        *,
        default_name: str,
        model: str,
        sw_version: str | None = None,
        connections: set[tuple[str, str]] | None = None,
        via_device: tuple[str, str] | None = None,
    ):
        super().__init__(id_, config_entry_or_id, {"identifiers": {(DOMAIN, id_)}})
        self.device_info = None
        self._online = False
        self._device_registry_entry = None
        with self.exception_warning("DeviceRegistry.async_get_or_create"):
            self._device_registry_entry = weakref.ref(
                device_registry.async_get(ApiProfile.hass).async_get_or_create(
                    config_entry_id=self.config_entry_id,
                    connections=connections,
                    manufacturer=mc.MANUFACTURER,
                    name=default_name,
                    model=model,
                    sw_version=sw_version,
                    via_device=via_device,
                    **self.deviceentry_id,
                )
            )

    # interface: EntityManager
    @property
    def name(self) -> str:
        """
        returns a proper (friendly) device name for logging purposes
        """
        if _device_registry_entry := self.device_registry_entry:
            return (
                _device_registry_entry.name_by_user
                or _device_registry_entry.name
                or self._get_internal_name()
            )
        return self._get_internal_name()

    @property
    def online(self):
        return self._online

    async def async_shutdown(self):
        await super().async_shutdown()
        self._device_registry_entry = None
        self.device_info = None

    # interface: self
    @property
    def device_registry_entry(self):
        _device_registry_entry = (
            self._device_registry_entry and self._device_registry_entry()
        )
        if _device_registry_entry is None:
            _device_registry_entry = device_registry.async_get(
                ApiProfile.hass
            ).async_get_device(**self.deviceentry_id)
            if _device_registry_entry:
                self._device_registry_entry = weakref.ref(_device_registry_entry)
        return _device_registry_entry

    def update_device_info(self, device_info: DeviceInfoType | SubDeviceInfoType):
        self.device_info = device_info
        if _device_registry_entry := self.device_registry_entry:
            name = (
                device_info.get(self._get_device_info_name_key())
                or self._get_internal_name()
            )
            if name != _device_registry_entry.name:
                device_registry.async_get(ApiProfile.hass).async_update_device(
                    _device_registry_entry.id, name=name
                )

    def request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        ApiProfile.hass.async_create_task(
            self.async_request(namespace, method, payload, response_callback)
        )

    @abc.abstractmethod
    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        raise NotImplementedError

    @abc.abstractmethod
    def _get_device_info_name_key(self) -> str:
        return ""

    @abc.abstractmethod
    def _get_internal_name(self) -> str:
        return ""

    def _set_online(self):
        self.log(DEBUG, "back online!")
        self._online = True

    def _set_offline(self):
        self.log(DEBUG, "going offline!")
        self._online = False
        for entity in self.entities.values():
            entity.set_unavailable()


class MerossDevice(MerossDeviceBase):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    # these are set from ConfigEntry
    config: DeviceConfigType
    polling_period: int
    _polling_delay: int
    conf_protocol: str
    pref_protocol: str
    curr_protocol: str
    # other default property values
    _tzinfo: ZoneInfo | None  # smart cache of device tzinfo
    _unsub_polling_callback: asyncio.TimerHandle | None

    entity_dnd: MerossEntity
    sensor_protocol: ProtocolSensor
    sensor_signal_strength: MLSensor
    update_firmware: MLUpdate | None

    __slots__ = (
        "polling_period",
        "_polling_delay",
        "conf_protocol",
        "pref_protocol",
        "curr_protocol",
        "descriptor",
        "needsave",
        "device_timestamp",
        "device_timedelta",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "device_debug",
        "lastrequest",
        "lastresponse",
        "_cloud_profile",
        "_mqtt_connection",  # we're binded to an MQTT profile/broker
        "_mqtt_connected",  # the broker is online/connected
        "_mqtt_publish",  # the broker accepts 'publish' (cloud broker conf might disable publishing)
        "_mqtt_active",  # the broker receives valid traffic i.e. the device is 'mqtt' reachable
        "_mqtt_lastrequest",
        "_mqtt_lastresponse",
        "_mqtt_transactions",
        "_http",  # cached MerossHttpClient
        "_http_active",  # HTTP is 'online' i.e. reachable
        "_http_lastrequest",
        "_http_lastresponse",
        "_trace_file",
        "_trace_future",
        "_trace_data",
        "_trace_endtime",
        "_trace_ability_iter",
        "polling_dictionary",
        "_tzinfo",
        "_unsub_polling_callback",
        "_queued_poll_requests",
        "entity_dnd",
        "sensor_protocol",
        "sensor_signal_strength",
        "update_firmware",
    )

    def __init__(
        self,
        descriptor: MerossDeviceDescriptor,
        config_entry: ConfigEntry,
    ):
        self.descriptor = descriptor
        self.needsave = False
        self.device_timestamp = 0.0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.device_debug = {}
        self.lastrequest = 0
        self.lastresponse = 0
        self._cloud_profile: MerossCloudProfile | None = None
        self._mqtt_connection: MQTTConnection | None = None
        self._mqtt_connected: MQTTConnection | None = None
        self._mqtt_publish: MQTTConnection | None = None
        self._mqtt_active: MQTTConnection | None = None
        self._mqtt_lastrequest = 0
        self._mqtt_lastresponse = 0
        self._http: MerossHttpClient | None = None
        self._http_active: MerossHttpClient | None = None
        self._http_lastrequest = 0
        self._http_lastresponse = 0
        self._trace_file: TextIOWrapper | None = None
        self._trace_future: asyncio.Future | None = None
        self._trace_data: list | None = None
        self._trace_endtime = 0
        self._trace_ability_iter = None
        # This is mainly for HTTP based devices: we build a dictionary of what we think could be
        # useful to asynchronously poll so the actual polling cycle doesnt waste time in checks
        # TL:DR we'll try to solve everything with just NS_SYS_ALL since it usually carries the full state
        # in a single transaction. Also (see #33) the multiplug mss425 doesnt publish the full switch list state
        # through NS_CNTRL_TOGGLEX (not sure if it's the firmware or the dialect)
        # Even if some devices don't carry significant state in NS_ALL we'll poll it anyway even if bulky
        # since it carries also timing informations and whatever
        self.polling_dictionary: dict[str, PollingStrategy] = {}
        self.polling_dictionary[mc.NS_APPLIANCE_SYSTEM_ALL] = PollingStrategy(
            mc.NS_APPLIANCE_SYSTEM_ALL
        )
        # Message handling is actually very hybrid:
        # when a message (device reply or originated) is received it gets routed to the
        # device instance in 'receive'. Here, it was traditionally parsed with a
        # switch structure against the different expected namespaces.
        # Now the architecture, while still in place, is being moved to handler methods
        # which are looked up by inspecting self for a proper '_handler_{namespace}' signature
        # This signature could be added at runtime or (better I guess) could be added by
        # dedicated mixin classes used to build the actual device class when the device is setup
        # (see __init__.MerossApi.build_device)
        # The handlers dictionary is anyway parsed first and could override a build-time handler.
        # The dicionary keys are Meross namespaces matched against when the message enters the handling
        # self.handlers: Dict[str, Callable] = {} actually disabled!

        # The list of pending MQTT requests (SET or GET) which are waiting their SETACK (or GETACK)
        # in order to complete the transaction
        self._mqtt_transactions: dict[str, _MQTTTransaction] = {}
        self._tzinfo = None
        self._unsub_polling_callback = None
        self._queued_poll_requests = 0

        # base init after setting some key properties needed for logging
        super().__init__(
            config_entry.data[CONF_DEVICE_ID],
            config_entry,
            default_name=descriptor.productname,
            model=descriptor.productmodel,
            sw_version=descriptor.firmwareVersion,
            connections={
                (device_registry.CONNECTION_NETWORK_MAC, descriptor.macAddress)
            },
        )

        self._update_config()
        self.curr_protocol = self.pref_protocol

        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in descriptor.ability:
            from .light import MLDNDLightEntity

            self.entity_dnd = MLDNDLightEntity(self)
            self.polling_dictionary[
                mc.NS_APPLIANCE_SYSTEM_DNDMODE
            ] = EntityPollingStrategy(mc.NS_APPLIANCE_SYSTEM_DNDMODE, self.entity_dnd)
        else:
            self.entity_dnd = MerossFakeEntity  # type: ignore

        self.sensor_protocol = ProtocolSensor(self)

        if mc.NS_APPLIANCE_SYSTEM_RUNTIME in descriptor.ability:
            self.sensor_signal_strength = sensor_signal_strength = MLSensor(
                self, None, "signal_strength", None
            )
            sensor_signal_strength._attr_entity_category = (
                MLSensor.EntityCategory.DIAGNOSTIC
            )
            sensor_signal_strength._attr_native_unit_of_measurement = PERCENTAGE
            sensor_signal_strength._attr_icon = "mdi:wifi"
            self.polling_dictionary[
                mc.NS_APPLIANCE_SYSTEM_RUNTIME
            ] = EntityPollingStrategy(
                mc.NS_APPLIANCE_SYSTEM_RUNTIME,
                sensor_signal_strength,
                PARAM_SIGNAL_UPDATE_PERIOD,
            )
        else:
            self.sensor_signal_strength = MerossFakeEntity  # type: ignore

        # the update entity will only be instantiated 'on demand' since
        # we might not have this for devices not related to a cloud profile
        self.platforms[MLUpdate.PLATFORM] = None
        self.update_firmware = None

        for _key, _payload in descriptor.digest.items():
            # _init_xxxx methods provided by mixins
            _init_method_name = f"_init_{_key}"
            if _init := getattr(self, _init_method_name, None):
                if isinstance(_payload, list):
                    for p in _payload:
                        with self.exception_warning(_init_method_name):
                            _init(p)
                else:
                    with self.exception_warning(_init_method_name):
                        _init(_payload)

    # interface: Loggable
    def log(self, level: int, msg: str, *args, **kwargs):
        LOGGER.log(level, f"MerossDevice({self.name}): {msg}", *args, **kwargs)
        if self._trace_file:
            self._trace(time(), msg % args, logging_getLevelName(level), "LOG")

    def warning(self, msg: str, *args, **kwargs):
        LOGGER.warning(f"MerossDevice({self.name}): {msg}", *args, **kwargs)
        if self._trace_file:
            self._trace(time(), msg % args, "WARNING", "LOG")

    # interface: EntityManager
    @callback
    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        await super().entry_update_listener(hass, config_entry)
        self._update_config()

        self._check_mqtt_connection_attach()

        if self.conf_protocol is not CONF_PROTOCOL_AUTO:
            if self.curr_protocol is not self.conf_protocol:
                self._switch_protocol(self.conf_protocol)

        if http := self._http:
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                self._http = self._http_active = None
                self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)
            else:
                http.key = self.key
                if host := self.host:
                    http.host = host

        # We'll activate debug tracing only when the user turns it on in OptionsFlowHandler so we usually
        # don't care about it on startup ('_set_config_entry'). When updating ConfigEntry
        # we always reset the timeout and so the trace will (eventually) restart
        if self._trace_file:
            self._trace_close()
        endtime = config_entry.data.get(CONF_TRACE, 0)
        epoch = time()
        if endtime > epoch:
            self._trace_open(epoch, endtime)
        # config_entry update might come from DHCP or OptionsFlowHandler address update
        # so we'll eventually retry querying the device
        if not self._online:
            await self.async_request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    # interface: MerossDeviceBase
    async def async_shutdown(self):
        """
        called when the config entry is unloaded
        we'll try to clear everything here
        """
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        if self._cloud_profile:
            self._cloud_profile.unlink(self)
        while self._unsub_polling_callback is None:
            # wait for the polling loop to finish in case
            await asyncio.sleep(1)
        self._unsub_polling_callback.cancel()
        self._unsub_polling_callback = None
        self.polling_dictionary.clear()
        if self._trace_file:
            self._trace_close()
        self.entity_dnd = None  # type: ignore
        self.sensor_signal_strength = None  # type: ignore
        self.sensor_protocol = None  # type: ignore
        self.update_firmware = None
        await super().async_shutdown()
        ApiProfile.devices[self.id] = None

    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        """
        route the request through MQTT or HTTP to the physical device.
        callback will be called on successful replies and actually implemented
        only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
        confirmation/status updates
        """
        self.lastrequest = time()
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self._mqtt_publish:
                await self.async_mqtt_request(
                    namespace, method, payload, response_callback
                )
                return
            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                return
            # protocol is AUTO
            self._switch_protocol(CONF_PROTOCOL_HTTP)

        # curr_protocol is HTTP
        if not await self.async_http_request(
            namespace, method, payload, callback=response_callback, attempts=3
        ):
            if (
                self._mqtt_active
                and self._mqtt_publish
                and (self.conf_protocol is CONF_PROTOCOL_AUTO)
            ):
                await self.async_mqtt_request(
                    namespace, method, payload, response_callback
                )

    def _get_device_info_name_key(self) -> str:
        return mc.KEY_DEVNAME

    def _get_internal_name(self) -> str:
        return self.descriptor.productname

    def _set_online(self):
        super()._set_online()
        self._polling_delay = self.polling_period
        self.sensor_protocol.update_connected()
        # retrigger the polling loop since we're already
        # scheduling an immediate async_request_updates.
        # This is needed to avoid startup staggering and also
        # as an optimization against asynchronous onlining events (on MQTT)
        # which could come anytime and so the (next)
        # polling might be too early
        if self._unsub_polling_callback:
            # might be None when we're already inside a polling loop
            self._unsub_polling_callback.cancel()
            self._unsub_polling_callback = schedule_async_callback(
                ApiProfile.hass, self._polling_delay, self._async_polling_callback
            )

    def _set_offline(self):
        super()._set_offline()
        self._polling_delay = self.polling_period
        self._mqtt_active = self._http_active = None
        for strategy in self.polling_dictionary.values():
            strategy.lastrequest = 0

    # interface: self
    @property
    def host(self):
        return self.config.get(CONF_HOST) or self.descriptor.innerIp

    @property
    def profile_id(self):
        profile_id = self.descriptor.userId
        return (
            profile_id if profile_id in ApiProfile.profiles else CONF_PROFILE_ID_LOCAL
        )

    @property
    def tzname(self):
        return self.descriptor.timezone

    @property
    def tzinfo(self):
        tz_name = self.descriptor.timezone
        if not tz_name:
            return timezone.utc
        if self._tzinfo and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
            return self._tzinfo
        except Exception:
            self.warning(
                "unable to load timezone info for %s - check your python environment",
                tz_name,
                timeout=14400,
            )
            self._tzinfo = None
        return timezone.utc

    @property
    def mqtt_locallyactive(self):
        """
        reports if the device is actively paired to a private (non-meross) MQTT
        in order to decide if we can/should send over a local MQTT with good
        chances of success.
        we should also check if the _mqtt_connection is 'publishable' but
        at the moment the MerossApi MQTTConnection doesn't allow disabling it
        """
        return self._mqtt_active and self._mqtt_active.profile is ApiProfile.api

    @property
    def mqtt_broker(self) -> tuple[str, int]:
        # deciding which broker to connect to might prove to be hard
        # since devices might fail-over the mqtt connection between 2 hosts
        def _safe_port(p_dict: dict, key: str) -> int:
            try:
                return int(p_dict[key]) or mc.MQTT_DEFAULT_PORT
            except Exception:
                return mc.MQTT_DEFAULT_PORT

        if p_debug := self.device_debug:
            # we have 'current' connection info so this should be very trustable
            with self.exception_warning(
                "mqtt_broker - parsing current brokers info", timeout=10
            ):
                p_cloud = p_debug[mc.KEY_CLOUD]
                active_server = p_cloud[mc.KEY_ACTIVESERVER]
                if active_server == p_cloud[mc.KEY_MAINSERVER]:
                    return str(active_server), _safe_port(p_cloud, mc.KEY_MAINPORT)
                elif active_server == p_cloud[mc.KEY_SECONDSERVER]:
                    return str(active_server), _safe_port(p_cloud, mc.KEY_SECONDPORT)

        fw = self.descriptor.firmware
        return str(fw[mc.KEY_SERVER]), _safe_port(fw, mc.KEY_PORT)

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll register mqtt listening (in case) and start polling after
        # the states have been eventually restored (some entities need this)
        # since mqtt could be readily available (it takes very few
        # tenths of sec to connect, setup and respond to our GET
        # NS_ALL) we'll give it a short 'advantage' before starting
        # the polling loop
        self._check_mqtt_connection_attach()

        self._unsub_polling_callback = schedule_async_callback(
            ApiProfile.hass,
            PARAM_COLDSTARTPOLL_DELAY if self._mqtt_connection else 0,
            self._async_polling_callback,
        )

    def get_datetime(self, epoch):
        """
        given the epoch (utc timestamp) returns the datetime
        in device local timezone
        """
        return datetime_from_epoch(epoch, self.tzinfo)

    async def async_request_smartpoll(
        self,
        epoch: float,
        lastupdate: float | int,
        polling_args: tuple,
        polling_period_min: int,
        polling_period_cloud: int = PARAM_CLOUDMQTT_UPDATE_PERIOD,
    ):
        if (epoch - lastupdate) < polling_period_min:
            return False
        if (
            self.pref_protocol is CONF_PROTOCOL_HTTP
            or self.curr_protocol is CONF_PROTOCOL_HTTP
        ):
            # this is likely the scenario for Meross cloud MQTT
            # or in general local devices with HTTP conf
            # we try HTTP first without any protocol auto-switching...
            if await self.async_http_request(*polling_args):
                return True
            # going on we should rely on MQTT but we skip it
            # and all of the autoswitch logic if not feasible
            if not self._mqtt_publish:
                return False
        if self.mqtt_locallyactive or (
            (self._queued_poll_requests == 0)
            and ((epoch - lastupdate) > polling_period_cloud)
        ):
            await self.async_request(*polling_args)
            self._queued_poll_requests += 1
            return True
        return False

    async def async_request_updates(self, epoch: float, namespace: str | None):
        """
        This is a 'versatile' polling strategy called on timer
        or when the device comes online (passing in the received namespace)
        'namespace' is 'None' when we're handling a scheduled polling when
        the device is online. When 'namespace' is not 'None' it represents the event
        of the device coming online following a succesful received message. This is
        likely to be 'NS_ALL', since it's the only message we request when offline.
        If we're connected to an MQTT broker anyway it could be any 'PUSH' message
        """
        if self.conf_protocol is CONF_PROTOCOL_AUTO and (not self._mqtt_active):
            # this is a special feature to use only on AUTO in order to see
            # if the device is 'mqtt connected' and where to
            await self.async_http_request(
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_DEBUG)
            )
        """
        we'll use _queued_poll_requests to track how many polls went through
        over MQTT for this cycle in order to only send 1 for each if we're
        binded to a cloud MQTT broker (in order to reduce bursts).
        If a poll request is discarded because of this, it should go through
        on the next polling cycle. This will 'spread' smart requests over
        subsequent polls
        """
        self._queued_poll_requests = 0
        for _strategy in self.polling_dictionary.values():
            if not self._online:
                return
            await _strategy(self, epoch, namespace)

    def receive(self, header: dict, payload: dict, protocol) -> bool:
        """
        default (received) message handling entry point
        """
        self.lastresponse = epoch = time()
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]

        if self._trace_file:
            self._trace(epoch, payload, namespace, method, protocol, TRACE_DIRECTION_RX)
        # we'll use the device timestamp to 'align' our time to the device one
        # this is useful for metered plugs reporting timestamped energy consumption
        # and we want to 'translate' this timings in our (local) time.
        # We ignore delays below PARAM_TIMESTAMP_TOLERANCE since
        # we'll always be a bit late in processing
        self.device_timestamp = float(header.get(mc.KEY_TIMESTAMP, epoch))
        device_timedelta = epoch - self.device_timestamp
        if abs(device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            self._config_timestamp(epoch, device_timedelta)
        else:
            self.device_timedelta = 0

        if MEROSSDEBUG:
            # it appears sometimes the devices
            # send an incorrect signature hash
            # but at the moment this is unlikely to be critical
            sign = get_message_signature(
                header[mc.KEY_MESSAGEID], self.key, header[mc.KEY_TIMESTAMP]
            )
            if sign != header[mc.KEY_SIGN]:
                self.warning(
                    "received signature error: computed=%s, header=%s",
                    sign,
                    json_dumps(header),
                )

        if not self._online:
            self._set_online()
            ApiProfile.hass.async_create_task(
                self.async_request_updates(epoch, namespace)
            )

        if method == mc.METHOD_ERROR:
            self.warning(
                "protocol error: namespace = '%s' payload = '%s'",
                namespace,
                json_dumps(payload),
                timeout=14400,
            )
            return True

        if namespace in self.polling_dictionary:
            # this might turn a 'SmartPollingStrategy' in something
            # even smarter if we receive a PUSH on this namespace
            # or if the namespace is queried out of polling loop
            self.polling_dictionary[namespace].lastrequest = epoch

        if method == mc.METHOD_SETACK:
            # SETACK generally don't carry any state/info so it is
            # no use parsing..moreover, our callbacks system is full
            # in place so we have no need to further process
            return True
        # disable this code: it is no use so far....
        # handler = self.handlers.get(namespace)
        # if handler is not None:
        #     handler(header, payload)
        #     return True
        with self.exception_warning("handle %s %s", method, namespace, timeout=14400):
            getattr(
                self, f"_handle_{namespace.replace('.', '_')}", self._handle_undefined
            )(header, payload)
            return True

        return False

    def _parse__generic(self, key: str, payload, entitykey: str | None = None):
        if isinstance(payload, dict):
            # we'll use an 'unsafe' access to payload[mc.KEY_CHANNEL]
            # so to better diagnose issues with non-standard payloads
            # we were previously using a safer approach but that could hide
            # unforeseen behaviours
            entity = self.entities[
                payload[mc.KEY_CHANNEL]
                if entitykey is None
                else f"{payload[mc.KEY_CHANNEL]}_{entitykey}"
            ]
            getattr(entity, f"_parse_{key}", entity._parse_undefined)(payload)
        elif isinstance(payload, list):
            for p in payload:
                self._parse__generic(key, p, entitykey)

    def _handle_undefined(self, header: dict, payload: dict):
        self.log(
            DEBUG,
            "handler undefined for method:(%s) namespace:(%s) payload:(%s)",
            header[mc.KEY_METHOD],
            header[mc.KEY_NAMESPACE],
            payload,
        )

    def _handle_generic(self, header: dict, payload: dict):
        """
        This is a basic implementation for dynamic protocol handlers
        since most of the payloads just need to extract a key and
        pass along to entities
        """
        key = get_namespacekey(header[mc.KEY_NAMESPACE])
        self._parse__generic(key, payload[key])

    def _parse__generic_array(self, key: str, payload, entitykey: str | None = None):
        # optimized version for well-known payloads which carry channel structs
        # play it safe for empty (None) payloads
        for channel_payload in payload or []:
            entity = self.entities[
                channel_payload[mc.KEY_CHANNEL]
                if entitykey is None
                else f"{channel_payload[mc.KEY_CHANNEL]}_{entitykey}"
            ]
            getattr(entity, f"_parse_{key}", entity._parse_undefined)(channel_payload)

    def _handle_generic_array(self, header: dict, payload: dict):
        """
        This is a basic implementation for dynamic protocol handlers
        since most of the payloads just need to extract a key and
        pass along to entities
        """
        key = get_namespacekey(header[mc.KEY_NAMESPACE])
        self._parse__generic_array(key, payload[key])

    def _handle_Appliance_System_All(self, header: dict, payload: dict):
        descr = self.descriptor
        oldfirmware = descr.firmware
        descr.update(payload)

        if oldfirmware != descr.firmware:
            # persist changes to configentry only when relevant properties change
            self.needsave = True
            if update_firmware := self.update_firmware:
                # self.update_firmware is dynamically created only when the cloud api
                # reports a newer fw
                update_firmware._attr_installed_version = descr.firmwareVersion
                if update_firmware._hass_connected:
                    update_firmware._async_write_ha_state()

        if self._mqtt_active:
            if not is_device_online(descr.system):
                self._mqtt_active = None
                self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_MQTT)
        elif self._mqtt_connected and is_device_online(descr.system):
            try:
                if self._mqtt_connected.broker == self.mqtt_broker:
                    self._mqtt_active = self._mqtt_connected
                    self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
            except Exception:
                pass

        if self.mqtt_locallyactive:
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            if self.device_timedelta and mc.NS_APPLIANCE_SYSTEM_CLOCK in descr.ability:
                # timestamp misalignment: try to fix it
                # only when devices are paired on our MQTT
                self.mqtt_request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})

            if mc.NS_APPLIANCE_SYSTEM_TIME in descr.ability:
                # check the appliance timeoffsets are updated (see #36)
                self._config_timezone(int(self.lastresponse), descr.time.get(mc.KEY_TIMEZONE))  # type: ignore

        for _key, _value in descr.digest.items():
            if _parse := getattr(self, f"_parse_{_key}", None):
                _parse(_value)
        # older firmwares (MSS110 with 1.1.28) look like
        # carrying 'control' instead of 'digest'
        if isinstance(p_control := descr.all.get(mc.KEY_CONTROL), dict):
            for _key, _value in p_control.items():
                if _parse := getattr(self, f"_parse_{_key}", None):
                    _parse(_value)

        if self.needsave:
            self.needsave = False
            self._save_config_entry(payload)

    def _handle_Appliance_System_Debug(self, header: dict, payload: dict):
        self.device_debug = p_debug = payload[mc.KEY_DEBUG]
        self.sensor_signal_strength.update_state(p_debug[mc.KEY_NETWORK][mc.KEY_SIGNAL])

    def _handle_Appliance_System_Runtime(self, header: dict, payload: dict):
        self.sensor_signal_strength.update_state(payload[mc.KEY_RUNTIME][mc.KEY_SIGNAL])

    def _handle_Appliance_System_DNDMode(self, header: dict, payload: dict):
        self.entity_dnd.update_onoff(payload[mc.KEY_DNDMODE][mc.KEY_MODE])

    def _handle_Appliance_System_Clock(self, header: dict, payload: dict):
        # this is part of initial flow over MQTT
        # we'll try to set the correct time in order to avoid
        # having NTP opened to setup the device
        # Note: I actually see this NS only on mss310 plugs
        # (msl120j bulb doesnt have it)
        if self.mqtt_locallyactive and (header[mc.KEY_METHOD] == mc.METHOD_PUSH):
            self.mqtt_request(
                mc.NS_APPLIANCE_SYSTEM_CLOCK,
                mc.METHOD_PUSH,
                {mc.KEY_CLOCK: {mc.KEY_TIMESTAMP: int(time())}},
            )

    def _handle_Appliance_System_Time(self, header: dict, payload: dict):
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            self.descriptor.update_time(payload[mc.KEY_TIME])

    def _handle_Appliance_Control_Bind(self, header: dict, payload: dict):
        """
        this transaction was observed on a trace from a msh300hk
        the device keeps sending 'SET'-'Bind' so I'm trying to
        kindly answer a 'SETACK'
        assumption is we're working on mqtt
        """
        if self.mqtt_locallyactive and (header[mc.KEY_METHOD] == mc.METHOD_SET):
            self.mqtt_request(
                mc.NS_APPLIANCE_CONTROL_BIND,
                mc.METHOD_SETACK,
                {},
                None,
                header[mc.KEY_MESSAGEID],
            )

    def mqtt_receive(self, header: dict, payload: dict):
        assert self._mqtt_connected and (self.conf_protocol is not CONF_PROTOCOL_HTTP)
        messageid = header[mc.KEY_MESSAGEID]
        if messageid in self._mqtt_transactions:
            mqtt_transaction = self._mqtt_transactions[messageid]
            if mqtt_transaction.namespace == header[mc.KEY_NAMESPACE]:
                self._mqtt_transactions.pop(messageid)
                mqtt_transaction.response_callback(
                    header[mc.KEY_METHOD] != mc.METHOD_ERROR, header, payload
                )
        if not self._mqtt_active:
            self._mqtt_active = self._mqtt_connection
            if self._online:
                self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
        if self.curr_protocol is not CONF_PROTOCOL_MQTT:
            if (self.pref_protocol is CONF_PROTOCOL_MQTT) or (not self._http_active):
                self._switch_protocol(CONF_PROTOCOL_MQTT)
        self.receive(header, payload, CONF_PROTOCOL_MQTT)
        self._mqtt_lastresponse = self.lastresponse

    def mqtt_attached(self, mqtt_connection: MQTTConnection):
        self.log(DEBUG, "mqtt_attached to %s", mqtt_connection.logtag)
        self._mqtt_connection = mqtt_connection
        if mqtt_connection.mqtt_is_connected:
            self.mqtt_connected()

    def mqtt_detached(self):
        assert self._mqtt_connection
        self.log(DEBUG, "mqtt_detached from %s", self._mqtt_connection.logtag)
        if self._mqtt_connected:
            self.mqtt_disconnected()
        self._mqtt_connection = None

    def mqtt_connected(self):
        _mqtt_connection = self._mqtt_connection
        assert _mqtt_connection
        self.log(DEBUG, "mqtt_connected to %s:%d", *_mqtt_connection.broker)
        self._mqtt_connected = _mqtt_connection
        if _mqtt_connection.allow_mqtt_publish:
            self._mqtt_publish = _mqtt_connection
        elif self.conf_protocol is CONF_PROTOCOL_MQTT:
            self.warning(
                "MQTT connection doesn't allow publishing - device will not be able send commands",
                timeout=14400,
            )
        self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT_BROKER)

    def mqtt_disconnected(self):
        assert self._mqtt_connection
        self.log(DEBUG, "mqtt_disconnected from %s:%d", *self._mqtt_connection.broker)
        self._mqtt_connected = self._mqtt_publish = self._mqtt_active = None
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self.conf_protocol is CONF_PROTOCOL_AUTO:
                self._switch_protocol(CONF_PROTOCOL_HTTP)
                return
            # conf_protocol should be CONF_PROTOCOL_MQTT:
            elif self._online:
                self._set_offline()
                return
        # run this at the end so it will not double flush
        self.sensor_protocol.update_attrs_inactive(
            ProtocolSensor.ATTR_MQTT_BROKER, ProtocolSensor.ATTR_MQTT
        )

    def mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
        messageid: str | None = None,
    ):
        ApiProfile.hass.async_create_task(
            self.async_mqtt_request(
                namespace, method, payload, response_callback, messageid
            )
        )

    async def async_mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
        messageid: str | None = None,
    ):
        if not self._mqtt_publish:
            # even if we're smart enough to not call async_mqtt_request when no mqtt
            # available, it could happen we loose that when asynchronously coming here
            self.log(
                DEBUG,
                "attempting to use async_mqtt_request with no publishing profile",
            )
            return
        if response_callback:
            transaction = _MQTTTransaction(namespace, method, response_callback)
            self._mqtt_transactions[transaction.messageid] = transaction
            messageid = transaction.messageid
        self._mqtt_lastrequest = time()
        if self._trace_file:
            self._trace(
                self._mqtt_lastrequest,
                payload,
                namespace,
                method,
                CONF_PROTOCOL_MQTT,
                TRACE_DIRECTION_TX,
            )
        await self._mqtt_publish.async_mqtt_publish(
            self.id, namespace, method, payload, self.key, messageid
        )

    async def async_http_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        *,
        callback: ResponseCallbackType | None = None,
        attempts: int = 1,
    ):
        with self.exception_warning(
            "async_http_request %s %s",
            method,
            namespace,
            timeout=14400,
        ):
            if not (http := self._http):
                http = MerossHttpClient(
                    self.host, self.key, async_get_clientsession(ApiProfile.hass), LOGGER  # type: ignore
                )
                self._http = http

            for attempt in range(attempts):
                # since we get 'random' connection errors, this is a retry attempts loop
                # until we get it done. We'd want to break out early on specific events tho (Timeouts)
                self._http_lastrequest = time()
                if self._trace_file:
                    self._trace(
                        self._http_lastrequest,
                        payload,
                        namespace,
                        method,
                        CONF_PROTOCOL_HTTP,
                        TRACE_DIRECTION_TX,
                    )
                try:
                    response = await http.async_request(namespace, method, payload)
                    break
                except Exception as exception:
                    self.log_exception(
                        DEBUG,
                        exception,
                        "async_http_request %s %s attempt(%d)",
                        method,
                        namespace,
                        attempt,
                    )
                    if not self._online:
                        return None
                    if self._http_active and namespace is mc.NS_APPLIANCE_SYSTEM_ALL:
                        self._http_active = None
                        self.sensor_protocol.update_attr_inactive(
                            ProtocolSensor.ATTR_HTTP
                        )
                    if isinstance(exception, asyncio.TimeoutError):
                        return None
                    await asyncio.sleep(0.1)  # wait a bit before re-issuing request
            else:
                return None

            if not self._http_active:
                self._http_active = http
                self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_HTTP)
            if self.curr_protocol is not CONF_PROTOCOL_HTTP:
                if (self.pref_protocol is CONF_PROTOCOL_HTTP) or (
                    not self._mqtt_active
                ):
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
            r_header = response[mc.KEY_HEADER]
            r_payload = response[mc.KEY_PAYLOAD]
            if callback:
                # we're actually only using this for SET->SETACK command confirmation
                callback(
                    r_header[mc.KEY_METHOD] != mc.METHOD_ERROR, r_header, r_payload
                )
            self.receive(r_header, r_payload, CONF_PROTOCOL_HTTP)
            self._http_lastresponse = self.lastresponse
            return response

        return None

    @callback
    async def _async_polling_callback(self):
        self.log(DEBUG, "polling start")
        try:
            self._unsub_polling_callback = None
            epoch = time()
            # this is a kind of 'heartbeat' to check if the device is still there
            # especially on MQTT where we might see no messages for a long time
            # This is also triggered at device setup to immediately request a fresh state
            # if ((epoch - self.lastrequest) > PARAM_HEARTBEAT_PERIOD) and (
            #    (epoch - self.lastupdate) > PARAM_HEARTBEAT_PERIOD
            # ):
            #    await self.async_request_get(mc.NS_APPLIANCE_SYSTEM_ALL)
            #   return
            if self._mqtt_transactions:
                # check and cleanup stale transactions
                _mqtt_transaction_stale_list = None
                for _mqtt_transaction in self._mqtt_transactions.values():
                    if (epoch - _mqtt_transaction.request_time) > 15:
                        if _mqtt_transaction_stale_list is None:
                            _mqtt_transaction_stale_list = []
                        _mqtt_transaction_stale_list.append(_mqtt_transaction.messageid)
                if _mqtt_transaction_stale_list:
                    for messageid in _mqtt_transaction_stale_list:
                        self._mqtt_transactions.pop(messageid)

            if self._online:
                # evaluate device availability by checking lastrequest got answered in less than polling_period
                if (self.lastresponse > self.lastrequest) or (
                    (epoch - self.lastrequest) < (self.polling_period - 2)
                ):
                    pass
                # when we 'fall' offline while on MQTT eventually retrigger HTTP.
                # the reverse is not needed since we switch HTTP -> MQTT right-away
                # when HTTP fails (see async_request)
                elif (self.conf_protocol is CONF_PROTOCOL_AUTO) and (
                    self.curr_protocol is not CONF_PROTOCOL_HTTP
                ):
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
                else:
                    self._set_offline()
                    return

                # assert self._online
                # when mqtt is working as a fallback for HTTP
                # we should periodically check if http comes back
                # in case our self.pref_protocol is HTTP.
                # when self.pref_protocol is MQTT we don't care
                # since we'll just try the switch when mqtt fails
                if (
                    (self.curr_protocol is CONF_PROTOCOL_MQTT)
                    and (self.pref_protocol is CONF_PROTOCOL_HTTP)
                    and ((epoch - self._http_lastrequest) > PARAM_HEARTBEAT_PERIOD)
                ):
                    await self.async_http_request(
                        *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                    )
                    # going on, should the http come online, the next
                    # async_request_updates will be 'smart' again, skipping
                    # state updates coming through mqtt (since we're still
                    # connected) but now requesting over http as preferred

                if self.mqtt_locallyactive:
                    # implement an heartbeat since mqtt might
                    # be unused for quite a bit
                    if (epoch - self._mqtt_lastresponse) > PARAM_HEARTBEAT_PERIOD:
                        self._mqtt_active = None
                        await self.async_mqtt_request(
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                        )
                        # this is rude..we would want to async wait on
                        # the mqtt response but we lack the infrastructure
                        await asyncio.sleep(2)
                        if not self._mqtt_active:
                            self.sensor_protocol.update_attr_inactive(
                                ProtocolSensor.ATTR_MQTT
                            )
                        # going on could eventually try/switch to HTTP

                await self.async_request_updates(epoch, None)

            else:  # offline
                if self._polling_delay < PARAM_HEARTBEAT_PERIOD:
                    self._polling_delay = self._polling_delay + self.polling_period
                else:
                    self._polling_delay = PARAM_HEARTBEAT_PERIOD

                ns_all_request_args = get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                if self.conf_protocol is CONF_PROTOCOL_AUTO:
                    if self.host:
                        if await self.async_http_request(*ns_all_request_args):
                            return
                    if self._mqtt_publish:
                        await self.async_mqtt_request(*ns_all_request_args)
                elif self.conf_protocol is CONF_PROTOCOL_MQTT:
                    if self._mqtt_publish:
                        await self.async_mqtt_request(*ns_all_request_args)
                else:  # self.conf_protocol is CONF_PROTOCOL_HTTP:
                    await self.async_http_request(*ns_all_request_args)
        finally:
            self._unsub_polling_callback = schedule_async_callback(
                ApiProfile.hass, self._polling_delay, self._async_polling_callback
            )
            self.log(DEBUG, "polling end")

    def entry_option_setup(self, config_schema: dict):
        """
        called when setting up an OptionsFlowHandler to expose
        configurable device preoperties which are stored at the device level
        and not at the configuration/option level
        see derived implementations
        """
        if self.mqtt_locallyactive and (
            mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability
        ):
            global TIMEZONES_SET
            if TIMEZONES_SET is None:
                try:
                    import zoneinfo

                    TIMEZONES_SET = zoneinfo.available_timezones()
                except Exception:
                    pass
                if TIMEZONES_SET:
                    TIMEZONES_SET = vol.In(sorted(TIMEZONES_SET))
                else:
                    # if error or empty try fallback to pytz if avail
                    try:
                        from pytz import common_timezones

                        TIMEZONES_SET = vol.In(sorted(common_timezones))
                    except Exception:
                        TIMEZONES_SET = str
            config_schema[
                vol.Optional(
                    mc.KEY_TIMEZONE,
                    description={"suggested_value": self.descriptor.timezone},
                )
            ] = TIMEZONES_SET

    def entry_option_update(self, user_input: DeviceConfigType):
        """
        called when the user 'SUBMIT' an OptionsFlowHandler: here we'll
        receive the full user_input so to update device config properties
        (this is actually called in sequence with entry_update_listener
        just the latter is async)
        """
        if self.mqtt_locallyactive and (
            mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability
        ):
            self._config_timezone(int(time()), user_input.get(mc.KEY_TIMEZONE))

    def _config_timestamp(self, epoch, device_timedelta):
        if abs(self.device_timedelta - device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            self.device_timedelta = device_timedelta
        else:  # average the sampled timedelta
            self.device_timedelta = (4 * self.device_timedelta + device_timedelta) / 5
        if self.mqtt_locallyactive and (
            mc.NS_APPLIANCE_SYSTEM_CLOCK in self.descriptor.ability
        ):
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            last_config_delay = epoch - self.device_timedelta_config_epoch
            if last_config_delay > 1800:
                # 30 minutes 'cooldown' in order to avoid restarting
                # the procedure too often
                self.mqtt_request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})
                self.device_timedelta_config_epoch = epoch
                return
            if last_config_delay < 30:
                # 30 sec 'deadzone' where we allow the timestamp
                # transaction to complete (should really be like few seconds)
                return
        if (epoch - self.device_timedelta_log_epoch) > 604800:  # 1 week lockout
            self.device_timedelta_log_epoch = epoch
            self.warning(
                "incorrect timestamp: %d seconds behind HA",
                int(self.device_timedelta),
            )

    def _config_timezone(self, epoch, tzname):
        p_time = self.descriptor.time
        assert p_time and self.mqtt_locallyactive
        p_timerule: list = p_time.get(mc.KEY_TIMERULE, [])
        p_timezone = p_time.get(mc.KEY_TIMEZONE)
        """
        timeRule should contain 2 entries: the actual time offsets and
        the next (incoming). If 'now' is after 'incoming' it means the
        first entry became stale and so we'll update the daylight offsets
        to current/next DST time window
        """
        if (p_timezone != tzname) or len(p_timerule) < 2 or p_timerule[1][0] < epoch:
            if tzname:
                """
                we'll look through the list of transition times for current tz
                and provide the actual (last past daylight) and the next to the
                appliance so it knows how and when to offset utc to localtime
                """
                timerules = []
                try:
                    import bisect

                    import pytz

                    tz_local = pytz.timezone(tzname)
                    idx = bisect.bisect_right(
                        tz_local._utc_transition_times,  # type: ignore
                        datetime.utcfromtimestamp(epoch),
                    )
                    # idx would be the next transition offset index
                    _transition_info = tz_local._transition_info[idx - 1]  # type: ignore
                    timerules.append(
                        [
                            int(tz_local._utc_transition_times[idx - 1].timestamp()),  # type: ignore
                            int(_transition_info[0].total_seconds()),
                            1 if _transition_info[1].total_seconds() else 0,
                        ]
                    )
                    _transition_info = tz_local._transition_info[idx]  # type: ignore
                    timerules.append(
                        [
                            int(tz_local._utc_transition_times[idx].timestamp()),  # type: ignore
                            int(_transition_info[0].total_seconds()),
                            1 if _transition_info[1].total_seconds() else 0,
                        ]
                    )
                except Exception as e:
                    self.warning(
                        "error while building timezone info (%s)",
                        str(e),
                    )
                    timerules = [[0, 0, 0], [epoch + PARAM_TIMEZONE_CHECK_PERIOD, 0, 1]]

                self.mqtt_request(
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                    mc.METHOD_SET,
                    payload={
                        mc.KEY_TIME: {
                            mc.KEY_TIMEZONE: tzname,
                            mc.KEY_TIMERULE: timerules,
                        }
                    },
                )
            elif p_timezone:  # and !timezone
                self.mqtt_request(
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                    mc.METHOD_SET,
                    payload={mc.KEY_TIME: {mc.KEY_TIMEZONE: "", mc.KEY_TIMERULE: []}},
                )

    def _switch_protocol(self, protocol):
        self.log(
            DEBUG,
            "switching protocol to %s",
            protocol,
        )
        self.curr_protocol = protocol
        if self._online:
            self.sensor_protocol.update_connected()

    def _save_config_entry(self, payload: dict):
        with self.exception_warning("ConfigEntry update"):
            entries = ApiProfile.hass.config_entries
            if entry := entries.async_get_entry(self.config_entry_id):
                data = dict(entry.data)
                data[CONF_PAYLOAD].update(payload)
                data[CONF_TIMESTAMP] = time()  # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)

    def _update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        config = self.config
        self.conf_protocol = CONF_PROTOCOL_OPTIONS.get(
            config.get(CONF_PROTOCOL), CONF_PROTOCOL_AUTO
        )
        if self.conf_protocol is CONF_PROTOCOL_AUTO:
            # When using CONF_PROTOCOL_AUTO we try to use our 'preferred' (pref_protocol)
            # and eventually fallback (curr_protocol) until some good news allow us
            # to retry pref_protocol. When binded to a cloud_profile always prefer
            # 'local' http since it should be faster and less prone to cloud 'issues'
            if config.get(CONF_HOST) or self.profile_id:
                self.pref_protocol = CONF_PROTOCOL_HTTP
            else:
                self.pref_protocol = CONF_PROTOCOL_MQTT
        else:
            self.pref_protocol = self.conf_protocol

        self.polling_period = (
            config.get(CONF_POLLING_PERIOD) or CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < CONF_POLLING_PERIOD_MIN:
            self.polling_period = CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

    def profile_linked(self, profile: MerossCloudProfile):
        if self._cloud_profile is not profile:
            if self._mqtt_connection:
                self._mqtt_connection.detach(self)
            if self._cloud_profile:
                self._cloud_profile.unlink(self)
            self._cloud_profile = profile
            self._check_mqtt_connection_attach()

    def profile_unlinked(self):
        # assert self._cloud_profile
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        self._cloud_profile = None

    def _check_mqtt_connection_attach(self):
        if self.conf_protocol is CONF_PROTOCOL_HTTP:
            # strictly HTTP so detach MQTT in case
            if self._mqtt_connection:
                self._mqtt_connection.detach(self)
        else:
            profile_id = self.profile_id
            if self._mqtt_connection:
                if self._mqtt_connection.profile.id == profile_id:
                    return
                self._mqtt_connection.detach(self)

            if profile_id:
                if self._cloud_profile:
                    self._cloud_profile.attach_mqtt(self)
            else:
                # this is the case for when we just want local handling
                # in this scenario we bind anyway to our local mqtt api
                # even tho the device might be unavailable since it's
                # still meross cloud bound. Also, we might not have
                # local mqtt at all but should this come later we don't
                # want to have to broadcast a connection event 'in the wild'
                # We should further inspect how to discriminate which
                # devices to add (or not) as a small optimization so to not
                # fill our local mqtt structures with unuseful data..
                # Right now add anyway since it's no harm
                # (no mqtt messages will come though)
                ApiProfile.api.attach_mqtt(self)

    def update_latest_version(self, latest_version: LatestVersionType):
        if not (update_firmware := self.update_firmware):
            self.update_firmware = update_firmware = MLUpdate(self)
        update_firmware._attr_installed_version = self.descriptor.firmwareVersion
        update_firmware._attr_latest_version = latest_version.get(mc.KEY_VERSION)
        update_firmware._attr_release_summary = latest_version.get(mc.KEY_DESCRIPTION)
        if update_firmware._hass_connected:
            update_firmware._async_write_ha_state()

    def get_diagnostics_trace(self, trace_timeout) -> asyncio.Future:
        """
        invoked by the diagnostics callback:
        here we set the device to start tracing the classical way (in file)
        but we also fill in a dict which will set back as the result of the
        Future we're returning to dignostics
        """
        if self._trace_future:
            # avoid re-entry..keep going the running trace
            return self._trace_future
        if self._trace_file:
            self._trace_close()
        self._trace_future = asyncio.get_running_loop().create_future()
        self._trace_data = []
        self._trace_data.append(
            ["time", "rxtx", "protocol", "method", "namespace", "data"]
        )
        epoch = time()
        self._trace_open(epoch, epoch + (trace_timeout or CONF_TRACE_TIMEOUT_DEFAULT))
        return self._trace_future

    def _trace_open(self, epoch: float, endtime):
        try:
            self.log(DEBUG, "start tracing")
            tracedir = ApiProfile.hass.config.path(
                "custom_components", DOMAIN, CONF_TRACE_DIRECTORY
            )
            os.makedirs(tracedir, exist_ok=True)
            self._trace_file = open(
                os.path.join(
                    tracedir,
                    CONF_TRACE_FILENAME.format(self.descriptor.type, int(endtime)),
                ),
                mode="w",
                encoding="utf8",
            )
            self._trace_endtime = endtime
            self._trace(
                epoch, self.descriptor.all, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GETACK
            )
            self._trace(
                epoch,
                self.descriptor.ability,
                mc.NS_APPLIANCE_SYSTEM_ABILITY,
                mc.METHOD_GETACK,
            )
            self._trace_ability_iter = iter(self.descriptor.ability)
            self._trace_ability()
        except Exception as exception:
            if self._trace_file:
                self._trace_close()
            self.log_exception_warning(exception, "creating trace file")

    def _trace_close(self):
        try:
            self._trace_file.close()  # type: ignore
            self._trace_file = None
        except Exception as exception:
            self._trace_file = None
            self.log_exception_warning(exception, "closing trace file")
        self._trace_ability_iter = None
        if self._trace_future:
            self._trace_future.set_result(self._trace_data)
            self._trace_future = None
        self._trace_data = None

    @callback
    def _trace_ability(self):
        if self._trace_ability_iter is None:
            return
        try:
            while True:
                ability: str = next(self._trace_ability_iter)
                if ability not in TRACE_ABILITY_EXCLUDE:
                    self.request(*get_default_arguments(ability))
                    break
            schedule_callback(
                ApiProfile.hass, PARAM_TRACING_ABILITY_POLL_TIMEOUT, self._trace_ability
            )
        except Exception:  # finished ?!
            self._trace_ability_iter = None

    def _trace(
        self,
        epoch: float,
        data: str | dict,
        namespace: str,
        method: str,
        protocol=CONF_PROTOCOL_AUTO,
        rxtx="",
    ):
        # assert self._trace_file is not None:
        try:
            if (epoch > self._trace_endtime) or (
                self._trace_file.tell() > CONF_TRACE_MAXSIZE  # type: ignore
            ):  # type: ignore
                self._trace_close()
                return

            if isinstance(data, dict):
                # we'll eventually make a deepcopy since data
                # might be retained by the _trace_data list
                # and carry over the deobfuscation (which we'll skip now)
                data = obfuscated_dict_copy(data)
                textdata = json_dumps(data)
            else:
                textdata = data
            texttime = strftime("%Y/%m/%d - %H:%M:%S", localtime(epoch))
            columns = [texttime, rxtx, protocol, method, namespace, textdata]
            self._trace_file.write("\t".join(columns) + "\r\n")  # type: ignore
            if self._trace_data is not None:
                # better have json for dignostic trace
                columns[5] = data  # type: ignore
                self._trace_data.append(columns)
        except Exception as exception:
            self._trace_close()
            self.log_exception_warning(exception, "writing to trace file")
