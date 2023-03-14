from __future__ import annotations
import typing
from logging import (
    WARNING,
    INFO,
    DEBUG,
    getLevelName as logging_getLevelName,
)
import os
import socket
import asyncio
from time import localtime, strftime, time
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo
from uuid import uuid4
from io import TextIOWrapper
from json import dumps as json_dumps
from copy import deepcopy
import voluptuous as vol
import weakref

from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry

from .helpers import (
    LOGGER,
    LOGGER_trap,
    obfuscate,
)
from .merossclient import (
    const as mc,  # mEROSS cONST
    get_namespacekey,
    get_replykey,
    get_default_arguments,
)
from .merossclient.httpclient import MerossHttpClient
from .meross_entity import MerossFakeEntity
from .sensor import ProtocolSensor
from .const import (
    DOMAIN,
    DeviceConfigType,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_CLOUD_PROFILE_ID,
    CONF_PAYLOAD,
    CONF_HOST,
    CONF_TIMESTAMP,
    CONF_POLLING_PERIOD,
    CONF_POLLING_PERIOD_DEFAULT,
    CONF_POLLING_PERIOD_MIN,
    CONF_PROTOCOL,
    CONF_PROTOCOL_OPTIONS,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_MQTT,
    CONF_PROTOCOL_HTTP,
    CONF_TRACE,
    CONF_TRACE_DIRECTORY,
    CONF_TRACE_FILENAME,
    CONF_TRACE_MAXSIZE,
    CONF_TRACE_TIMEOUT_DEFAULT,
    PARAM_COLDSTARTPOLL_DELAY,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_TIMEZONE_CHECK_PERIOD,
    PARAM_TIMESTAMP_TOLERANCE,
    PARAM_TRACING_ABILITY_POLL_TIMEOUT,
)

ResponseCallbackType = typing.Callable[[bool, dict, dict], None]

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from . import MerossApi
    from .merossclient import MerossDeviceDescriptor
    from .meross_entity import MerossEntity

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

    namespace: str
    method: str
    response_callback: ResponseCallbackType
    messageid: str
    request_time: float

    def __init__(
        self, namespace: str, method: str, response_callback: ResponseCallbackType
    ):
        self.namespace = namespace
        self.method = method
        self.response_callback = response_callback
        self.request_time = time()
        self.messageid = uuid4().hex


class MerossDevice:
    """
    Generic protocol handler class managing the physical device stack/state
    """

    # provide class defaults for typing:
    sensor_protocol: ProtocolSensor
    # these are set from ConfigEntry
    _host: str | None = None
    key: str = ""
    polling_period: int = CONF_POLLING_PERIOD_DEFAULT
    _polling_delay: int = CONF_POLLING_PERIOD_DEFAULT
    conf_protocol: str
    pref_protocol: str
    curr_protocol: str
    # other default property values
    entity_dnd = MerossFakeEntity
    _deviceentry = None  # weakly cached entry to the device registry
    _tzinfo: ZoneInfo | None = None # smart cache of device tzinfo

    def __init__(
        self,
        api: MerossApi,
        descriptor: MerossDeviceDescriptor,
        config_entry: ConfigEntry,
    ):
        self.device_id: str = config_entry.data[CONF_DEVICE_ID]
        LOGGER.debug("MerossDevice(%s) init", self.device_id)
        self.api = api
        self.hass = api.hass
        self.descriptor = descriptor
        self.entry_id = config_entry.entry_id
        self.needsave = True  # after boot update with fresh CONF_PAYLOAD
        self.device_timestamp = 0.0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self._online = False
        self.lastrequest = 0
        self.lastresponse = 0
        self._cloud_profile_id = None
        self._cloud_profile = None
        self._mqtt_profile = None
        self._mqtt = None
        self.lastmqttrequest = 0
        self.lastmqttresponse = 0
        self._http: MerossHttpClient | None = None
        self.lasthttprequest = 0
        self.lasthttpresponse = 0
        self._trace_file: TextIOWrapper | None = None
        self._trace_future: asyncio.Future | None = None
        self._trace_data: list | None = None
        self._trace_endtime = 0
        self._trace_ability_iter = None
        # This is a collection of all of the instanced entities
        # they're generally built here during __init__ and will be registered
        # in platforms(s) async_setup_entry with HA
        self.entities: dict[object, "MerossEntity"] = {}
        # This is mainly for HTTP based devices: we build a dictionary of what we think could be
        # useful to asynchronously poll so the actual polling cycle doesnt waste time in checks
        # TL:DR we'll try to solve everything with just NS_SYS_ALL since it usually carries the full state
        # in a single transaction. Also (see #33) the multiplug mss425 doesnt publish the full switch list state
        # through NS_CNTRL_TOGGLEX (not sure if it's the firmware or the dialect)
        # Even if some devices don't carry significant state in NS_ALL we'll poll it anyway even if bulky
        # since it carries also timing informations and whatever
        self.polling_dictionary: dict[str, dict] = {}
        self.polling_dictionary[mc.NS_APPLIANCE_SYSTEM_ALL] = mc.PAYLOAD_GET[
            mc.NS_APPLIANCE_SYSTEM_ALL
        ]
        # when we build an entity we also add the relative platform name here
        # so that the async_setup_entry for the integration will be able to forward
        # the setup to the appropriate platform.
        # The item value here will be set to the async_add_entities callback
        # during the corresponding platform async_setup_entry so to be able
        # to dynamically add more entities should they 'pop-up' (Hub only?)
        self.platforms: dict[str, typing.Callable | None] = {}
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

        # cache this so entities will just ref it
        self.device_info_id = {"identifiers": {(DOMAIN, self.device_id)}}
        try:
            # try block since this is not critical
            deviceentry = device_registry.async_get(api.hass).async_get_or_create(
                config_entry_id=config_entry.entry_id,
                connections={
                    (device_registry.CONNECTION_NETWORK_MAC, descriptor.macAddress)
                },
                manufacturer=mc.MANUFACTURER,
                name=descriptor.productname,
                model=descriptor.productmodel,
                sw_version=descriptor.firmware.get(mc.KEY_VERSION),
                **self.device_info_id,
            )
            self._deviceentry = weakref.ref(deviceentry)
        except:
            pass

        self.sensor_protocol = ProtocolSensor(self)

        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in descriptor.ability:
            from .light import MLDNDLightEntity

            self.entity_dnd = MLDNDLightEntity(self)

        for key, payload in descriptor.digest.items():
            # _init_xxxx methods provided by mixins
            _init = getattr(self, f"_init_{key}", None)
            if _init is not None:
                if isinstance(payload, list):
                    for p in payload:
                        try:
                            _init(p)
                        except:# TODO: add debug log
                            pass
                else:
                    try:
                        _init(payload)
                    except:# TODO: add debug log
                        pass

        self._unsub_entry_update_listener = config_entry.add_update_listener(
            self.entry_update_listener
        )
        self._set_config_entry(config_entry.data)  # type: ignore
        self.curr_protocol = self.pref_protocol

    def __del__(self):
        LOGGER.debug("MerossDevice(%s) destroy", self.device_id)
        return

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll start polling after the states have been eventually
        # restored (some entities need this)
        # since mqtt could be readily available (it takes very few
        # tenths of sec to connect, setup and respond to our GET
        # NS_ALL) we'll give it a short 'advantage' before starting
        # the polling loop
        if self.conf_protocol is not CONF_PROTOCOL_HTTP:
            self._mqtt_profile_attach()

        self._unsub_polling_callback = self.api.schedule_async_callback(
            0 if self._mqtt_profile is None else PARAM_COLDSTARTPOLL_DELAY,
            self._async_polling_callback,
        )

    async def async_shutdown(self):
        """
        called when the config entry is unloaded
        we'll try to clear everything here
        """
        if self._mqtt_profile is not None:
            self._mqtt_profile_detach()
        if self._unsub_entry_update_listener is not None:
            self._unsub_entry_update_listener()
            self._unsub_entry_update_listener = None
        while self._unsub_polling_callback is None:
            # wait for the polling loop to finish in case
            await asyncio.sleep(1)
        self._unsub_polling_callback.cancel()
        self._unsub_polling_callback = None
        if self._trace_file is not None:
            self._trace_close()
        self.entities.clear()
        self.entity_dnd = MerossFakeEntity
        self.sensor_protocol = None  # type: ignore

    @property
    def host(self):
        return self._host or self.descriptor.innerIp

    @property
    def tzname(self):
        return self.descriptor.timezone

    @property
    def tzinfo(self) -> tzinfo:
        tz_name = self.descriptor.timezone
        if not tz_name:
            return timezone.utc
        if (self._tzinfo is not None) and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
            return self._tzinfo
        except Exception:
            self.log(
                WARNING,
                14400,
                "MerossDevice(%s) unable to load timezone info for %s - check your python environment",
                self.name,
                tz_name,
            )
            self._tzinfo = None
        return timezone.utc

    @property
    def name(self) -> str:
        """
        returns a proper (friendly) device name for logging purposes
        """
        deviceentry = self._deviceentry and self._deviceentry()
        if deviceentry is None:
            deviceentry = device_registry.async_get(self.hass).async_get_device(
                identifiers={(DOMAIN, self.device_id)}
            )
            if deviceentry is None:
                return self.descriptor.productname
            self._deviceentry = weakref.ref(deviceentry)

        return (
            deviceentry.name_by_user or deviceentry.name or self.descriptor.productname
        )

    @property
    def online(self):
        return self._online

    @property
    def locallybound(self):
        """
        should report if the device is paired to meros or not
        This should actually be inferred from system payload but at the moment
        we just euristically guess by the fact we're receiving 'local' MQTT
        """
        return self._mqtt == self.api

    def log(self, level: int, timeout: int, msg: str, *args):
        if timeout:
            LOGGER_trap(level, timeout, msg, *args)
        else:
            LOGGER.log(level, msg, *args)
        if self._trace_file is not None:
            self._trace(time(), msg % args, logging_getLevelName(level), "LOG")

    def request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        self.hass.async_create_task(
            self.async_request(namespace, method, payload, response_callback)
        )

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
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            # only publish when mqtt component is really connected else we'd
            # insanely dump lot of mqtt errors in log
            if self._mqtt is not None:
                await self.async_mqtt_request(
                    namespace, method, payload, response_callback
                )
                self.lastrequest = self.lastmqttrequest
                return
            # MQTT not connected
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                self.lastrequest = time()
                return
            # protocol is AUTO
            self._switch_protocol(CONF_PROTOCOL_HTTP)

        # curr_protocol is HTTP
        await self.async_http_request(namespace, method, payload, response_callback)
        self.lastrequest = self.lasthttprequest

    async def async_request_updates(self, epoch, namespace):
        """
        This is a 'versatile' polling strategy called on timer
        or when the device comes online (passing in the received namespace)
        When the device doesnt listen MQTT at all this will always fire the list of requests
        else, when MQTT is alive this will fire the requests only once when just switching online
        or when not listening any MQTT over the PARAM_HEARTBEAT_PERIOD
        """
        if (namespace is not None) or (self.lastmqttresponse == 0):
            for _namespace, _payload in self.polling_dictionary.items():
                if self._online:
                    if _namespace != namespace:
                        await self.async_request(_namespace, mc.METHOD_GET, _payload)
                else:
                    # it might happen we detect a timeout when using HTTP
                    # and this is interpreted as a clear indication the
                    # device is offline (see async_http_request) so we break
                    # the polling cycle and wait for a reconnect procedure
                    # without wasting execution time here
                    break
            if self._online and self.entity_dnd.enabled:
                await self.async_request(
                    *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_DNDMODE)
                )

    def receive(self, header: dict, payload: dict, protocol) -> bool:
        """
        default (received) message handling entry point
        """
        self.lastresponse = epoch = time()
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]
        if protocol is CONF_PROTOCOL_HTTP:
            self.lasthttpresponse = epoch
        else:
            self.lastmqttresponse = epoch

        if self._trace_file is not None:
            self._trace(epoch, payload, namespace, method, protocol, TRACE_DIRECTION_RX)
        if (self.pref_protocol is protocol) and (self.curr_protocol is not protocol):
            self._switch_protocol(protocol)
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

        if get_replykey(header, self.key) is not self.key:
            self.log(
                WARNING,
                14400,
                "MerossDevice(%s) received signature error (incorrect key?)",
                self.name,
            )

        if not self._online:
            self._set_online()
            self.hass.async_create_task(self.async_request_updates(epoch, namespace))

        if method == mc.METHOD_ERROR:
            self.log(
                WARNING,
                14400,
                "MerossDevice(%s) protocol error: namespace = '%s' payload = '%s'",
                self.name,
                namespace,
                json_dumps(payload),
            )
            return True

        # disable this code: it is no use so far....
        # handler = self.handlers.get(namespace)
        # if handler is not None:
        #     handler(header, payload)
        #     return True
        handler = getattr(self, f"_handle_{namespace.replace('.', '_')}", None)
        if handler is not None:
            handler(header, payload)
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

    def _handle_generic(self, header: dict, payload: dict):
        """
        This is a basic implementation for dynamic protocol handlers
        since most of the payloads just need to extract a key and
        pass along to entities
        """
        key = get_namespacekey(header[mc.KEY_NAMESPACE])
        self._parse__generic(key, payload.get(key))

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
        self._parse__generic_array(key, payload.get(key))

    def _handle_Appliance_System_All(self, header: dict, payload: dict):
        self._parse_all(payload)
        if self.needsave is True:
            self.needsave = False
            self._save_config_entry(payload)

    def _handle_Appliance_System_DNDMode(self, header: dict, payload: dict):
        if isinstance(dndmode := payload.get(mc.KEY_DNDMODE), dict):
            self.entity_dnd.update_onoff(dndmode.get(mc.KEY_MODE))  # type: ignore

    def _handle_Appliance_System_Clock(self, header: dict, payload: dict):
        # this is part of initial flow over MQTT
        # we'll try to set the correct time in order to avoid
        # having NTP opened to setup the device
        # Note: I actually see this NS only on mss310 plugs
        # (msl120j bulb doesnt have it)
        if self.locallybound and (header[mc.KEY_METHOD] == mc.METHOD_PUSH):
            self.mqtt_request(
                mc.NS_APPLIANCE_SYSTEM_CLOCK,
                mc.METHOD_PUSH,
                {mc.KEY_CLOCK: {mc.KEY_TIMESTAMP: int(time())}},
            )

    def _handle_Appliance_System_Time(self, header: dict, payload: dict):
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            self.descriptor.update_time(payload.get(mc.KEY_TIME, {}))

    def _handle_Appliance_Control_Bind(self, header: dict, payload: dict):
        """
        this transaction was observed on a trace from a msh300hk
        the device keeps sending 'SET'-'Bind' so I'm trying to
        kindly answer a 'SETACK'
        assumption is we're working on mqtt
        """
        if self.locallybound and (header[mc.KEY_METHOD] == mc.METHOD_SET):
            self.mqtt_request(
                mc.NS_APPLIANCE_CONTROL_BIND,
                mc.METHOD_SETACK,
                {},
                None,
                header[mc.KEY_MESSAGEID],
            )

    def mqtt_receive(self, header: dict, payload: dict):
        assert (self.conf_protocol is not CONF_PROTOCOL_HTTP) and (
            self._mqtt is not None
        )
        messageid = header[mc.KEY_MESSAGEID]
        if messageid in self._mqtt_transactions:
            mqtt_transaction = self._mqtt_transactions[messageid]
            if mqtt_transaction.namespace == header[mc.KEY_NAMESPACE]:
                self._mqtt_transactions.pop(messageid)
                mqtt_transaction.response_callback(
                    header[mc.KEY_METHOD] != mc.METHOD_ERROR, header, payload
                )
        if self._online and (self.lastmqttresponse == 0):
            self.sensor_protocol.update_connected_attr(ProtocolSensor.ATTR_MQTT)
        self.receive(header, payload, CONF_PROTOCOL_MQTT)

    def set_mqtt_connected(self):
        self._mqtt = self._mqtt_profile
        self.sensor_protocol.update_connected_attr(ProtocolSensor.ATTR_MQTT_BROKER)
        # even tho this req might seems redundant it serves
        # us to check if the mqtt channel is 'really' working
        self.mqtt_request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    def set_mqtt_disconnected(self):
        self._mqtt = None
        self.lastmqttresponse = 0
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self.conf_protocol is CONF_PROTOCOL_AUTO:
                self._switch_protocol(CONF_PROTOCOL_HTTP)
                return
            # conf_protocol should be CONF_PROTOCOL_MQTT:
            elif self._online:
                self._set_offline()
                return
        # run this at the end so it will not double flush
        self.sensor_protocol.update_disconnected_attr(
            (ProtocolSensor.ATTR_MQTT_BROKER, ProtocolSensor.ATTR_MQTT)
        )

    def mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
        messageid: str | None = None,
    ):
        self.hass.async_create_task(
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
        if self._mqtt is None:
            # even if we're smart enough to not call async_mqtt_request when no mqtt
            # available, it could happen we loose that when asynchronously coming here
            self.log(
                DEBUG,
                0,
                "MerossDevice(%s) attempting to use async_mqtt_request with no available profile",
                self.name,
            )
            return
        if response_callback is not None:
            transaction = _MQTTTransaction(namespace, method, response_callback)
            self._mqtt_transactions[transaction.messageid] = transaction
            messageid = transaction.messageid
        self.lastmqttrequest = time()
        if self._trace_file is not None:
            self._trace(
                self.lastmqttrequest,
                payload,
                namespace,
                method,
                CONF_PROTOCOL_MQTT,
                TRACE_DIRECTION_TX,
            )
        await self._mqtt.async_mqtt_publish(
            self.device_id, namespace, method, payload, self.key, messageid
        )

    async def async_http_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        try:
            if (http := self._http) is None:
                http = MerossHttpClient(
                    self.host, self.key, async_get_clientsession(self.hass), LOGGER  # type: ignore
                )
                self._http = http

            for attempt in range(3):
                # since we get 'random' connection errors, this is a retry attempts loop
                # until we get it done. We'd want to break out early on specific events tho (Timeouts)
                self.lasthttprequest = time()
                if self._trace_file is not None:
                    self._trace(
                        self.lasthttprequest,
                        payload,
                        namespace,
                        method,
                        CONF_PROTOCOL_HTTP,
                        TRACE_DIRECTION_TX,
                    )
                try:
                    response = await http.async_request(namespace, method, payload)
                    if self._online and (self.lasthttpresponse == 0):
                        self.sensor_protocol.update_connected_attr(
                            ProtocolSensor.ATTR_HTTP
                        )
                    break
                except Exception as e:
                    if not self._online:
                        raise e
                    self.lasthttpresponse = 0
                    self.log(
                        INFO,
                        0,
                        "MerossDevice(%s) %s(%s) in async_http_request %s %s attempt(%s)",
                        self.name,
                        type(e).__name__,
                        str(e),
                        method,
                        namespace,
                        str(attempt),
                    )
                    if (
                        self.conf_protocol is CONF_PROTOCOL_AUTO
                    ) and self.lastmqttresponse:
                        if self.curr_protocol is not CONF_PROTOCOL_MQTT:
                            self._switch_protocol(CONF_PROTOCOL_MQTT)
                        else:
                            self.sensor_protocol.update_disconnected_attr(
                                ProtocolSensor.ATTR_HTTP
                            )
                        await self.async_mqtt_request(
                            namespace, method, payload, response_callback
                        )
                        return
                    elif isinstance(e, asyncio.TimeoutError):
                        self._set_offline()
                        return
                    await asyncio.sleep(0.1)  # wait a bit before re-issuing request
            else:
                self.sensor_protocol.update_disconnected_attr(ProtocolSensor.ATTR_HTTP)
                return

            r_header = response[mc.KEY_HEADER]
            r_payload = response[mc.KEY_PAYLOAD]
            if response_callback is not None:
                # we're actually only using this for SET->SETACK command confirmation
                response_callback(
                    r_header[mc.KEY_METHOD] != mc.METHOD_ERROR, r_header, r_payload
                )
            self.receive(r_header, r_payload, CONF_PROTOCOL_HTTP)
        except Exception as e:
            self.log(
                WARNING,
                14400,
                "MerossDevice(%s) %s(%s) in async_http_request %s %s",
                self.name,
                type(e).__name__,
                str(e),
                method,
                namespace,
            )

    @callback
    async def _async_polling_callback(self):
        LOGGER.log(DEBUG, "MerossDevice(%s) polling start", self.name)
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
                if _mqtt_transaction_stale_list is not None:
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
                # when HTTP fails (see async_http_request)
                elif (self.conf_protocol is CONF_PROTOCOL_AUTO) and (
                    self.curr_protocol is not CONF_PROTOCOL_HTTP
                ):
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
                else:
                    self._set_offline()
                    return

                if self.lastmqttresponse:
                    # implement an heartbeat since mqtt might
                    # be unused for quite a bit when we 'prefer' HTTP
                    if (epoch - self.lastmqttresponse) > PARAM_HEARTBEAT_PERIOD:
                        self.lastmqttresponse = 0
                        await self.async_mqtt_request(
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                        )
                        # this is rude..we would want to async wait on
                        # the mqtt response but we lack the infrastructure
                        await asyncio.sleep(2)
                        if self.lastmqttresponse == 0:
                            self.sensor_protocol.update_disconnected_attr(
                                ProtocolSensor.ATTR_MQTT
                            )
                        # going on could eventually try/switch to HTTP
                    else:
                        # when mqtt is working as a fallback for HTTP
                        # we should periodically check if http comes back
                        # in case our self.pref_protocol is HTTP.
                        # when self.pref_protocol is MQTT we don't care
                        # since we'll just try the switch when mqtt fails
                        if (
                            (self.curr_protocol is CONF_PROTOCOL_MQTT)
                            and (self.pref_protocol is CONF_PROTOCOL_HTTP)
                            and (
                                (epoch - self.lasthttprequest) > PARAM_HEARTBEAT_PERIOD
                            )
                        ):
                            await self.async_http_request(
                                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                            )
                            # going on, should the http come online, the next
                            # async_request_updates will be 'smart' again, skipping
                            # state updates coming through mqtt (since we're still
                            # connected) but now requesting over http as preferred

                await self.async_request_updates(epoch, None)

            else:  # offline

                if self._polling_delay < PARAM_HEARTBEAT_PERIOD:
                    self._polling_delay = self._polling_delay + self.polling_period
                else:
                    self._polling_delay = PARAM_HEARTBEAT_PERIOD

                ns_method_payload = get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                if self.conf_protocol is CONF_PROTOCOL_AUTO:
                    if self.host:
                        await self.async_http_request(*ns_method_payload)
                        if self._online:
                            return
                    if self._mqtt is not None:
                        await self.async_mqtt_request(*ns_method_payload)
                elif self.conf_protocol is CONF_PROTOCOL_MQTT:
                    if self._mqtt is not None:
                        await self.async_mqtt_request(*ns_method_payload)
                else:  # self.conf_protocol is CONF_PROTOCOL_HTTP:
                    await self.async_http_request(*ns_method_payload)
        finally:
            self._unsub_polling_callback = self.api.schedule_async_callback(
                self._polling_delay, self._async_polling_callback
            )
            LOGGER.log(DEBUG, "MerossDevice(%s) polling end", self.name)

    def entry_option_setup(self, config_schema: dict):
        """
        called when setting up an OptionsFlowHandler to expose
        configurable device preoperties which are stored at the device level
        and not at the configuration/option level
        see derived implementations
        """
        if self.locallybound and (
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
        if (
            self._online
            and self.locallybound
            and (mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability)
        ):
            self._config_timezone(int(time()), user_input.get(mc.KEY_TIMEZONE))

    @callback
    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        """
        callback after user changed configuration through OptionsFlowHandler
        deviceid and/or host are not changed so we're still referring to the same device
        """
        self._set_config_entry(config_entry.data)  # type: ignore

        if self.conf_protocol is CONF_PROTOCOL_HTTP:
            # strictly HTTP so detach MQTT in case
            if self._mqtt_profile is not None:
                self._mqtt_profile_detach()
        else:
            self._mqtt_profile_attach()

        if self.conf_protocol is not CONF_PROTOCOL_AUTO:
            if self.curr_protocol is not self.pref_protocol:
                self._switch_protocol(self.pref_protocol)

        if (http := self._http) is not None:
            if self._host:
                http.host = self._host
            http.key = self.key
        # We'll activate debug tracing only when the user turns it on in OptionsFlowHandler so we usually
        # don't care about it on startup ('_set_config_entry'). When updating ConfigEntry
        # we always reset the timeout and so the trace will (eventually) restart
        if self._trace_file is not None:
            self._trace_close()
        endtime = config_entry.data.get(CONF_TRACE, 0)
        epoch = time()
        if endtime > epoch:
            self._trace_open(epoch, endtime)
        # config_entry update might come from DHCP or OptionsFlowHandler address update
        # so we'll eventually retry querying the device
        if not self._online:
            await self.async_request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    def _parse_all(self, payload: dict):
        """
        called internally when we receive an NS_SYSTEM_ALL
        i.e. global device setup/status
        we usually don't expect a 'structural' change in the device here
        except maybe for Hub(s) which we're going to investigate later
        set 'self.needsave' if we want to persist the payload to the ConfigEntry
        """
        descr = self.descriptor
        oldaddr = descr.innerIp
        descr.update(payload)
        # persist changes to configentry only when relevant properties change
        newaddr = descr.innerIp
        if newaddr and (oldaddr != newaddr):
            # check the new innerIp is good since we have random blanks in the wild (#90)
            try:
                socket.inet_aton(newaddr)
                # good enough..check if we're using an MQTT device (i.e. device with no CONF_HOST)
                # and eventually cache this value so we could use it when falling back to HTTP
                if not self._host:
                    if (http := self._http) is not None:
                        http.host = newaddr
                self.needsave = True
            except:
                pass

        epoch = int(
            self.lastresponse
        )  # we're not calling time() since it's fresh enough

        if self.locallybound:
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            if self.device_timedelta and mc.NS_APPLIANCE_SYSTEM_CLOCK in descr.ability:
                # timestamp misalignment: try to fix it
                # only when devices are paired on our MQTT
                self.request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})

            if mc.NS_APPLIANCE_SYSTEM_TIME in descr.ability:
                # check the appliance timeoffsets are updated (see #36)
                self._config_timezone(epoch, descr.time.get(mc.KEY_TIMEZONE))  # type: ignore

        for key, value in descr.digest.items():
            _parse = getattr(self, f"_parse_{key}", None)
            if _parse is not None:
                _parse(value)
        # older firmwares (MSS110 with 1.1.28) look like
        # carrying 'control' instead of 'digest'
        if isinstance(p_control := descr.all.get(mc.KEY_CONTROL), dict):
            for key, value in p_control.items():
                _parse = getattr(self, f"_parse_{key}", None)
                if _parse is not None:
                    _parse(value)

    def _config_timestamp(self, epoch, device_timedelta):
        if abs(self.device_timedelta - device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            self.device_timedelta = device_timedelta
        else:  # average the sampled timedelta
            self.device_timedelta = (4 * self.device_timedelta + device_timedelta) / 5
        if (
            self.locallybound
            and mc.NS_APPLIANCE_SYSTEM_CLOCK in self.descriptor.ability
        ):
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            last_config_delay = epoch - self.device_timedelta_config_epoch
            if last_config_delay > 1800:
                # 30 minutes 'cooldown' in order to avoid restarting
                # the procedure too often
                self.request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})
                self.device_timedelta_config_epoch = epoch
                return
            if last_config_delay < 30:
                # 30 sec 'deadzone' where we allow the timestamp
                # transaction to complete (should really be like few seconds)
                return
        if (epoch - self.device_timedelta_log_epoch) > 604800:  # 1 week lockout
            self.device_timedelta_log_epoch = epoch
            self.log(
                WARNING,
                0,
                "MerossDevice(%s) has incorrect timestamp: %d seconds behind HA",
                self.name,
                int(self.device_timedelta),
            )

    def _config_timezone(self, epoch, tzname):
        p_time = self.descriptor.time
        assert p_time is not None
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
                    import pytz
                    import bisect

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
                    self.log(
                        WARNING,
                        0,
                        "MerossDevice(%s) error while building timezone info (%s)",
                        self.name,
                        str(e),
                    )
                    timerules = [[0, 0, 0], [epoch + PARAM_TIMEZONE_CHECK_PERIOD, 0, 1]]

                self.request(
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
                self.request(
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                    mc.METHOD_SET,
                    payload={mc.KEY_TIME: {mc.KEY_TIMEZONE: "", mc.KEY_TIMERULE: []}},
                )

    def _set_online(self):
        self.log(DEBUG, 0, "MerossDevice(%s) back online!", self.name)
        self._online = True
        self._polling_delay = self.polling_period
        self.sensor_protocol.update_connected()
        # retrigger the polling loop since we're already
        # scheduling an immediate async_request_updates.
        # This is needed to avoid startup staggering and also
        # as an optimization against asynchronous onlining events (on MQTT)
        # which could come anytime and so the (next)
        # polling might be too early
        if self._unsub_polling_callback is not None:
            # might be None when we're already inside a polling loop
            self._unsub_polling_callback.cancel()
            self._unsub_polling_callback = self.api.schedule_async_callback(
                self._polling_delay, self._async_polling_callback
            )

    def _set_offline(self):
        self.log(DEBUG, 0, "MerossDevice(%s) going offline!", self.name)
        self._online = False
        self._polling_delay = self.polling_period
        self.lastmqttresponse = 0
        self.lasthttpresponse = 0
        for entity in self.entities.values():
            entity.set_unavailable()

    def _switch_protocol(self, protocol):
        self.log(
            INFO,
            0,
            "MerossDevice(%s) switching protocol to %s",
            self.name,
            protocol,
        )
        self.curr_protocol = protocol
        # preferred protocol is not treated simmetrically:
        # if we prefer http, we mainly use that (self._http)
        # but still we listen for pushes on mqtt so we
        # mantain self._mqtt (which could become stale inside our
        # logic since we're not generally using it and updates from
        # devices could come in along time). When we prefer mqtt
        # instead we can drop the self._http since it is likely
        # unused (an mqtt drop will try bring it online)
        if (protocol is CONF_PROTOCOL_MQTT) and (protocol is self.pref_protocol):
            self._http = None  # release memory...
            self.lasthttpresponse = 0  # signals http disconnected

        if self._online:
            self.sensor_protocol.update_connected()

    def _save_config_entry(self, payload: dict):
        try:
            entries = self.hass.config_entries
            entry = entries.async_get_entry(self.entry_id)
            if entry is not None:
                data = dict(entry.data)
                data[CONF_PAYLOAD].update(payload)
                data[CONF_TIMESTAMP] = time() # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
        except Exception as e:
            self.log(
                WARNING,
                0,
                "MerossDevice(%s) error while updating ConfigEntry (%s)",
                self.name,
                str(e),
            )

    def _set_config_entry(self, data: DeviceConfigType):
        """
        common properties read from ConfigEntry on __init__ or when a configentry updates
        """
        self._host = data.get(CONF_HOST)
        self.key = data.get(CONF_KEY) or ""
        self._cloud_profile_id = data.get(CONF_CLOUD_PROFILE_ID)
        self.conf_protocol = CONF_PROTOCOL_OPTIONS.get(
            data.get(CONF_PROTOCOL), CONF_PROTOCOL_AUTO
        )
        if self.conf_protocol is CONF_PROTOCOL_AUTO:
            # When using CONF_PROTOCOL_AUTO we try to use our 'preferred' (pref_protocol)
            # and eventually fallback (curr_protocol) until some good news allow us
            # to retry pref_protocol. When binded to a cloud_profile always prefer
            # 'local' http since it should be faster and less prone to cloud 'issues'
            if self._host or self._cloud_profile_id:
                self.pref_protocol = CONF_PROTOCOL_HTTP
            else:
                self.pref_protocol = CONF_PROTOCOL_MQTT
        else:
            self.pref_protocol = self.conf_protocol

        self.polling_period = (
            data.get(CONF_POLLING_PERIOD) or CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < CONF_POLLING_PERIOD_MIN:
            self.polling_period = CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period
        """
        if self.conf_protocol is CONF_PROTOCOL_HTTP:
            # strictly HTTP so no use of MQTT
            if self._mqtt_profile is not None:
                self._mqtt_profile_detach()
        elif _cloud_profile_id := data.get(CONF_CLOUD_PROFILE_ID):
            # this is the case for when we want to use meross cloud mqtt.
            # On config entry update we might be already connected to the right
            # cloud_profile...
            if self._cloud_profile_id != _cloud_profile_id:
                if self._mqtt_profile is not None:
                    self._mqtt_profile_detach()
                if self.descriptor.userId != _cloud_profile_id:
                    self.log(
                        WARNING,
                        0,
                        "MerossDevice(%s): cloud profile(%s) does not match device user(%s)",
                        self.name,
                        _cloud_profile_id,
                        str(self.descriptor.userId),
                    )
                else:
                    self._cloud_profile = self.api.profiles.get(_cloud_profile_id)
                    if self._cloud_profile is not None:
                        self._cloud_profile_id = _cloud_profile_id
                        self._mqtt_profile = self._cloud_profile.attach(self)
                        if self._mqtt_profile.mqtt_is_connected:
                            self.set_mqtt_connected()
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
            if self._mqtt_profile is not self.api:
                if self._mqtt_profile is not None:
                    self._mqtt_profile_detach()
                self._mqtt_profile = self.api.attach(self)
                if self._mqtt_profile.mqtt_is_connected:
                    self.set_mqtt_connected()
        """

    def _mqtt_profile_attach(self):
        if self._cloud_profile_id:
            # this is the case for when we want to use meross cloud mqtt.
            # On config entry update we might be already connected to the right
            # cloud_profile...
            if self._mqtt_profile is not None:
                if self._cloud_profile is None or self._cloud_profile.profile_id != self._cloud_profile_id:
                    self._mqtt_profile_detach()

            if self._mqtt_profile is None:
                if self.descriptor.userId != self._cloud_profile_id:
                    self.log(
                        WARNING,
                        0,
                        "MerossDevice(%s): cloud profile(%s) does not match device user(%s)",
                        self.name,
                        self._cloud_profile_id,
                        str(self.descriptor.userId),
                    )
                else:
                    self._cloud_profile = self.api.profiles.get(self._cloud_profile_id)
                    if self._cloud_profile is not None:
                        self._mqtt_profile = self._cloud_profile.attach(self)
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
            if self._mqtt_profile is not self.api:
                if self._mqtt_profile is not None:
                    self._mqtt_profile_detach()
                self._mqtt_profile = self.api.attach(self)

    def _mqtt_profile_detach(self):
        if self._mqtt_profile is not None:
            self._mqtt_profile.detach(self)
            self._mqtt_profile = None
            if self._mqtt is not None:
                self.set_mqtt_disconnected()
            self._cloud_profile = None

    def get_diagnostics_trace(self, trace_timeout) -> asyncio.Future:
        """
        invoked by the diagnostics callback:
        here we set the device to start tracing the classical way (in file)
        but we also fill in a dict which will set back as the result of the
        Future we're returning to dignostics
        """
        if self._trace_future is not None:
            # avoid re-entry..keep going the running trace
            return self._trace_future
        if self._trace_file is not None:
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
            LOGGER.debug("MerossDevice(%s): start tracing", self.name)
            tracedir = self.hass.config.path(
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
        except Exception as e:
            LOGGER.warning(
                "MerossDevice(%s) error while creating trace file (%s)",
                self.name,
                str(e),
            )
            if self._trace_file is not None:
                self._trace_close()

    def _trace_close(self):
        try:
            self._trace_file.close()  # type: ignore
        except Exception as e:
            LOGGER.warning(
                "MerossDevice(%s) error while closing trace file (%s)",
                self.name,
                str(e),
            )
        self._trace_file = None
        self._trace_ability_iter = None
        if self._trace_future is not None:
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
            self.api.schedule_callback(
                PARAM_TRACING_ABILITY_POLL_TIMEOUT, self._trace_ability
            )
        except:  # finished ?!
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
        # expect self._trace_file is not None:
        if (epoch > self._trace_endtime) or (
            self._trace_file.tell() > CONF_TRACE_MAXSIZE  # type: ignore
        ):  # type: ignore
            self._trace_close()
            return

        if isinstance(data, dict):
            # we'll eventually make a deepcopy since data
            # might be retained by the _trace_data list
            # and carry over the deobfuscation (which we'll skip now)
            data = deepcopy(data)
            obfuscate(data)
            textdata = json_dumps(data)
        else:
            textdata = data

        try:
            texttime = strftime("%Y/%m/%d - %H:%M:%S", localtime(epoch))
            columns = [texttime, rxtx, protocol, method, namespace, textdata]
            self._trace_file.write("\t".join(columns) + "\r\n")  # type: ignore
            if self._trace_data is not None:
                # better have json for dignostic trace
                columns[5] = data  # type: ignore
                self._trace_data.append(columns)
        except Exception as e:
            LOGGER.warning(
                "MerossDevice(%s) error while writing to trace file (%s)",
                self.name,
                str(e),
            )
            self._trace_close()
