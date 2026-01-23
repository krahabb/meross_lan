import asyncio
import importlib
from time import time
from typing import TYPE_CHECKING, override
import zoneinfo

from bleak.exc import BleakError
from homeassistant import const as hac
from homeassistant.components import bluetooth as ha_bt
from homeassistant.core import SupportsResponse, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

# import core modules instead of symbols to ease patching in a single place
from . import ConfigEntryType, device as mld, manager as mlm, mqtt_profile as mlq
from .. import const as mlc
from ..merossclient import (
    MEROSSDEBUG,
    HostAddress,
    MerossDeviceDescriptor,
    bluetooth as m_bt,
)
from ..merossclient.httpclient import MerossHttpClient
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import (
    MerossAckReply,
    MerossPushReply,
    MerossRequest,
    json_loads,
)

if TYPE_CHECKING:

    from typing import Callable, Final, Literal

    from homeassistant.components.mqtt import async_publish as mqtt_async_publish
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import (
        CALLBACK_TYPE,
        HomeAssistant,
        ServiceCall,
        ServiceResponse,
    )

    from ..config_flow import ConfigFlow
    from ..merossclient.protocol.message import MerossMessage
    from .device import Device
    from .meross_profile import MerossProfile

else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


MIXIN_DIGEST_INIT = {
    mc.KEY_HUB: (".devices.hub", "HubMixin"),
}


class HAMQTTConnection(mlq.MQTTConnection):

    if TYPE_CHECKING:
        is_cloud_connection: Final[Literal[False]]

        _unsub_mqtt_subscribe: Callable | None
        _unsub_mqtt_disconnected: Callable | None
        _unsub_mqtt_connected: Callable | None
        _mqtt_subscribe_future: asyncio.Future[bool] | None
        _unsub_random_disconnect: asyncio.TimerHandle | None

    __slots__ = (
        "_unsub_mqtt_subscribe",
        "_unsub_mqtt_disconnected",
        "_unsub_mqtt_connected",
        "_mqtt_subscribe_future",
        "_unsub_random_disconnect",
    )

    def __init__(self, api: "ComponentApi"):
        self.is_cloud_connection = False
        mlq.MQTTConnection.__init__(
            self,
            api,
            HostAddress("homeassistant", 0),
            mc.TOPIC_RESPONSE.format(mlc.DOMAIN),
        )
        self._unsub_mqtt_subscribe = None
        self._unsub_mqtt_disconnected = None
        self._unsub_mqtt_connected = None
        self._mqtt_subscribe_future = None
        if MEROSSDEBUG:

            async def _async_random_disconnect():
                self._unsub_random_disconnect = api.schedule_async_callback(
                    60, _async_random_disconnect
                )
                if self._mqtt_subscribe_future:
                    return
                elif self._unsub_mqtt_subscribe is None:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(self.DEBUG, "random connect")
                        await self.async_mqtt_subscribe()
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(self.DEBUG, "random disconnect")
                        await self.async_mqtt_unsubscribe()

            self._unsub_random_disconnect = api.schedule_async_callback(
                60, _async_random_disconnect
            )
        else:
            self._unsub_random_disconnect = None

    # interface: MQTTConnection
    async def async_shutdown(self):
        if self._unsub_random_disconnect:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        await self.async_mqtt_unsubscribe()
        await super().async_shutdown()

    def get_rl_safe_delay(self, uuid: str):
        return 0.0

    @override
    async def _async_mqtt_publish(self, request: "MerossMessage"):
        await mqtt_async_publish(
            self.profile.api.hass, mc.TOPIC_REQUEST.format(request.uuid), request.json
        )
        self._mqtt_published()

    # interface: self
    @property
    def mqtt_is_subscribed(self):
        return self._unsub_mqtt_subscribe is not None

    async def async_mqtt_subscribe(self) -> bool:
        if self._unsub_mqtt_subscribe:
            return True

        if self._mqtt_subscribe_future:
            return await self._mqtt_subscribe_future

        hass = self.profile.api.hass
        self._mqtt_subscribe_future = hass.loop.create_future()
        try:
            from homeassistant.components import mqtt

            global mqtt_async_publish
            mqtt_async_publish = mqtt.async_publish

            self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                hass, mc.TOPIC_DISCOVERY, self.async_mqtt_message
            )

            @callback
            def _connection_status_callback(connected: bool):
                if connected:
                    self._mqtt_connected()
                else:
                    self._mqtt_disconnected()

            try:
                # HA core 2024.6
                self._unsub_mqtt_connected = mqtt.async_subscribe_connection_status(
                    hass, _connection_status_callback
                )
            except:
                self._unsub_mqtt_disconnected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_DISCONNECTED, self._mqtt_disconnected  # type: ignore (removed in HA core 2024.6)
                )
                self._unsub_mqtt_connected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_CONNECTED, self._mqtt_connected  # type: ignore (removed in HA core 2024.6)
                )
            if mqtt.is_connected(hass):
                self._mqtt_connected()
            result = True
        except Exception as exception:
            self.log_exception(
                self.WARNING, exception, "async_mqtt_subscribe", timeout=14400
            )
            result = False

        self._mqtt_subscribe_future.set_result(result)
        self._mqtt_subscribe_future = None
        return result

    async def async_mqtt_unsubscribe(self):
        if self._mqtt_subscribe_future:
            await self._mqtt_subscribe_future
        if self._unsub_mqtt_connected:
            self._unsub_mqtt_connected()
            self._unsub_mqtt_connected = None
        if self._unsub_mqtt_disconnected:
            self._unsub_mqtt_disconnected()
            self._unsub_mqtt_disconnected = None
        if self._unsub_mqtt_subscribe:
            self._unsub_mqtt_subscribe()
            self._unsub_mqtt_subscribe = None
        if self._mqtt_is_connected:
            self._mqtt_disconnected()

    @callback
    def _mqtt_connected(self):
        """called when the underlying mqtt.Client connects to the broker"""
        # try to get the HA broker host address
        with self.exception_warning("async_mqtt_subscribe: recovering broker conf"):
            from homeassistant.components import mqtt

            mqtt_data = self.profile.api.hass.data[mqtt.DATA_MQTT]
            if mqtt_data and mqtt_data.client:
                conf = mqtt_data.client.conf
                self.broker.host = conf[mqtt.CONF_BROKER]
                self.broker.port = conf.get(hac.CONF_PORT, mqtt.const.DEFAULT_PORT)
                self.configure_logger()

        super()._mqtt_connected()

    # these handlers are used to manage session establishment on MQTT.
    # They are typically sent by the device when they connect to the broker
    # and they are used to mimic the official Meross brokers session managment
    # They're implemented at the MQTTConnection level since the device might not be
    # configured yet in meross_lan. When the device is configured, we still manage
    # these 'session messages' here but we'll forward them to the device too in order
    # to trigger all of the device connection management.
    async def _handle_Appliance_Control_Bind(self, message: "MerossMessage"):
        # this transaction appears when a device (firstly)
        # connects to an MQTT broker and tries to 'register'
        # itself. Our guess right now is to just SETACK
        # trying fix #346. When building the reply, the
        # meross broker sets the from field as
        # "from": "cloud/sub/kIGFRwvtAQP4sbXv/58c35d719350a689"
        # and the fields look like hashes or something since
        # they change between attempts (hashed broker id ?)
        # At any rate I don't have a clue on how to properly
        # replicate this and the "from" field is set as ususal

        device_id = message.uuid
        api = self.profile.api
        if device_id in api.devices:
            if device := api.devices[device_id]:
                key = device.key
            else:  # device not loaded...
                device_entry = api.get_config_entry(device_id)
                if device_entry:
                    key = device_entry.data.get(mlc.CONF_KEY) or ""
                else:
                    key = self.profile.key
        else:
            key = self.profile.key
        if message.method == mc.METHOD_SET:
            await self.async_mqtt_publish(
                MerossAckReply(
                    message,
                    {},
                    key,
                    mc.TOPIC_RESPONSE.format(device_id),
                ),
            )
        # keep forwarding the message
        return False

    async def _handle_Appliance_Control_ConsumptionConfig(
        self, message: "MerossMessage"
    ):
        # this message is published by mss switches
        # and it appears newer mss315 could abort their connection
        # if not replied (see #346)
        if message.method == mc.METHOD_PUSH:
            await self.async_mqtt_publish(
                MerossPushReply(message, message.payload),
            )
        # keep forwarding the message
        return False

    async def _handle_Appliance_System_Clock(self, message: "MerossMessage"):
        # this is part of initial flow over MQTT
        # we'll try to set the correct time in order to avoid
        # having NTP opened to setup the device
        # Note: I actually see this NS only on mss310 plugs
        # (msl120j bulb doesnt have it)
        if message.method == mc.METHOD_PUSH:
            await self.async_mqtt_publish(
                MerossPushReply(
                    message, {mc.KEY_CLOCK: {mc.KEY_TIMESTAMP: int(time())}}
                ),
            )
        # keep forwarding the message
        return False


HAMQTTConnection.SESSION_HANDLERS = {
    mn.Appliance_Control_Bind: HAMQTTConnection._handle_Appliance_Control_Bind,
    mn.Appliance_Control_ConsumptionConfig: HAMQTTConnection._handle_Appliance_Control_ConsumptionConfig,
    mn.Appliance_System_Clock: HAMQTTConnection._handle_Appliance_System_Clock,
    mn.Appliance_System_Online: HAMQTTConnection._handle_Appliance_System_Online,
}


class ComponentApi(mlq.MQTTProfile):
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """

    class BTDevice(m_bt.BluetoothClient):
        if TYPE_CHECKING:
            api: Final["ComponentApi"]
            address: Final[str]
            info: ha_bt.BluetoothServiceInfoBleak
            descriptor: Final[MerossDeviceDescriptor]  # type: ignore
            uuid: Final[str]  # BEWARE: not valid until _init_task done
            device: Final[Device | None]
            _flow_id: Final[str]
            _bt_unavailable_unsub: Final[CALLBACK_TYPE]
            _init_task: asyncio.Task["ComponentApi.BTDevice"]

        __slots__ = (
            "api",
            "address",
            "info",
            "uuid",
            "device",
            "_flow_id",
            "_bt_unavailable_unsub",
            "_init_task",
        )

        def __init__(self, api: "ComponentApi", address: str, flow_id: str, /):
            self.api = api
            self.getEffectiveLevel = api.getEffectiveLevel
            self.isEnabledFor = api.isEnabledFor
            self.address = address
            self.uuid = None  # type: ignore
            self.device = None
            self._flow_id = flow_id
            m_bt.BluetoothClient.__init__(
                self,
                address,
                from_=mlc.DOMAIN,
                loop=api.hass.loop,
                logger=self,
            )
            self._bt_unavailable_unsub = ha_bt.async_track_unavailable(
                api.hass, self._bt_unavailable, address, connectable=True
            )
            api._bt_devices[address] = self
            self._init_task = api.async_create_task(
                self._async_init(), f"BTDevice({address})._async_init", False
            )

        async def _async_init(self):
            api = self.api
            while True:
                try:
                    descriptor = await self.async_identify()
                    self.uuid = uuid = descriptor.uuid  # type: ignore
                    for _bt_device in api._bt_devices.values():
                        if (_bt_device is not self) and (_bt_device.uuid == uuid):
                            # an existing device has a new bt address
                            await _bt_device.async_shutdown()
                            break
                    # check to see if the device is one of our configureds
                    try:
                        device = api.devices[uuid]
                        if device:
                            conf_protocol = device.config.get(mlc.CONF_PROTOCOL)
                        else:
                            config_entry = api.get_config_entry(uuid)
                            assert config_entry
                            conf_protocol = config_entry.data.get(mlc.CONF_PROTOCOL)
                        if conf_protocol == mlc.CONF_PROTOCOL_BLUETOOTH:
                            # already configured to use BT
                            if device:
                                self.attach(device)
                            raise AbortFlow("already_configured")
                    except KeyError:
                        # device not configured yet..proceed with ConfigFlow
                        pass

                    return self
                except (TimeoutError, BleakError) as e:
                    self.log_exception(
                        ComponentApi.WARNING,
                        e,
                        "device identification. Retrying in 30 sec",
                    )
                    await self.disconnect()
                    await asyncio.sleep(30)

        async def async_shutdown(self):
            self._bt_unavailable_unsub()
            del self.api._bt_devices[self.address]
            if self.device:
                self.device.bt_detached()
                self.device = None  # type: ignore
            else:
                self._init_task.cancel()
            if self.is_connected:
                await self.disconnect()

        def attach(self, device: "Device"):
            if self.device:
                self.device.bt_detached()
            device.bt_attached(self)
            self.device = device  # type: ignore

        def detach(self):
            assert self.device
            self.device.bt_detached()
            self.device = None  # type: ignore

        def log(self, level: int, msg: str, *args, **kwargs):
            self.api.log(level, f"BTDevice({self.address}): {msg}", *args, **kwargs)

        def log_exception(
            self, level: int, exception: Exception, msg: str, *args, **kwargs
        ):
            self.log(
                level,
                f"{exception.__class__.__name__}({str(exception)}) in {msg}",
                *args,
                **kwargs,
            )

        """ REMOVE
        def update(self, info: ha_bt.BluetoothServiceInfoBleak):
            self.info = info
            if not self.uuid:
                try:
                    uuid = info.manufacturer_data[0xFFFF].hex()
                    if mc.RE_PATTERN_UUID.match(uuid):
                        self.uuid = uuid
                    else:
                        self.log(
                            ComponentApi.DEBUG,
                            "Malformed UUID in manufacturer data: %s",
                            uuid,
                        )
                except KeyError:
                    self.log(
                        ComponentApi.DEBUG,
                        "Missing UUID in manufacturer data: %r",
                        info.manufacturer_data,
                    )
            self.log(ComponentApi.DEBUG, "Updated service_info: %s", info)
        """

        @override
        def _on_connected(self):
            if self.device:
                self.device.bt_connected()

        @override
        def _on_disconnected(self):
            if self.device:
                self.device.bt_disconnected()

        @callback
        def _bt_unavailable(self, info: ha_bt.BluetoothServiceInfoBleak):
            self.log(
                ComponentApi.DEBUG, "_bt_unavailable_callback(service_info: %s)", info
            )
            self.api.async_create_task(
                self.async_shutdown(), "_bt_unavailable_callback"
            )

    if TYPE_CHECKING:
        hass: Final[HomeAssistant]

        devices: Final[dict[str, Device | None]]
        """
        dict of configured devices. Every device config_entry in the system is mapped here and
        set to the Device instance if the device is actually active (config_entry loaded)
        or set to None if the config_entry is not loaded (no device instance)
        """
        profiles: Final[dict[str, MerossProfile | None]]
        """
        dict of configured cloud profiles (behaves as the 'devices' dict).
        """
        managers_transient_state: Final[dict[str, dict]]
        """
        This is actually a temporary memory storage used to mantain some info related to
        an ConfigEntry/EntityManager that we don't want to persist to hass storage (useless overhead)
        since they're just runtime context but we need an independent storage than
        EntityManager since these info are needed during EntityManager async_setup_entry.
        See the tracing feature activated through the OptionsFlow for insights.
        """

        device_registry: Final[dr.DeviceRegistry]
        entity_registry: Final[er.EntityRegistry]
        issue_registry: Final[ir.IssueRegistry]
        # TODO: cache configentries and/or loop accessor?

        _mqtt_connection: HAMQTTConnection | None

        _deviceclasses: Final[dict[str, type[Device]]]
        _available_timezones: list[str] | None
        _zoneinfo: Final[dict[str, zoneinfo.ZoneInfo]]

        _bt_devices: Final[dict[str, BTDevice]]

        # Overrides
        is_cloud_profile: Final[Literal[False]]

    __slots__ = (
        "hass",
        "devices",
        "profiles",
        "managers_transient_state",
        "device_registry",
        "entity_registry",
        "issue_registry",
        "_mqtt_connection",
        "_deviceclasses",
        "_available_timezones",
        "_zoneinfo",
        "_import_module_lock",
        "_import_module_cache",
        "_bt_devices",
    )

    @staticmethod
    def get(hass: "HomeAssistant") -> "ComponentApi":
        """
        Set up the component.
        'Our' truth singleton is saved in hass.data[DOMAIN] and
        Loggable.api is just a cache to speed access
        """
        try:
            return hass.data[mlc.DOMAIN]
        except KeyError:
            return ComponentApi(hass)

    def active_devices(self):
        """Iterates over the currently loaded MerossDevices."""
        return (device for device in self.devices.values() if device)

    def active_profiles(self):
        """Iterates over the currently loaded MerossCloudProfiles."""
        return (profile for profile in self.profiles.values() if profile)

    def get_device_with_mac(self, macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in self.active_devices():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    def __init__(self, hass: "HomeAssistant"):
        self.hass = hass
        self.devices = {}
        self.profiles = {}
        self.managers_transient_state = {}
        self.device_registry = dr.async_get(hass)
        self.entity_registry = er.async_get(hass)
        self.issue_registry = ir.async_get(hass)
        self._mqtt_connection = None
        self._deviceclasses = {}
        self._available_timezones = None
        self._zoneinfo = {}
        self._import_module_lock = asyncio.Lock()
        self._import_module_cache = {}
        for config_entry in hass.config_entries.async_entries(mlc.DOMAIN):
            match ConfigEntryType.get_type_and_id(config_entry.unique_id):
                case (ConfigEntryType.DEVICE, device_id):
                    self.devices[device_id] = None
                case (ConfigEntryType.PROFILE, profile_id):
                    self.profiles[profile_id] = None
        self._bt_devices = {}
        self.api = self  # type: ignore
        self.is_cloud_profile = False
        mlq.MQTTProfile.__init__(
            self,
            self,
            mlc.CONF_PROFILE_ID_LOCAL,
            hass.config_entries.async_entry_for_domain_unique_id(
                mlc.DOMAIN, mlc.DOMAIN
            ),
        )

        async def _async_service_request(
            service_call: "ServiceCall",
        ) -> "ServiceResponse":
            service_response = {}
            device_id = service_call.data.get(mlc.CONF_DEVICE_ID)
            host = service_call.data.get(mlc.CONF_HOST)
            if not device_id and not host:
                raise HomeAssistantError(
                    "Missing both device_id and host: provide at least one valid entry"
                )
            protocol = (
                service_call.data.get(mlc.CONF_PROTOCOL) or mlc.CONF_PROTOCOL_AUTO
            )
            namespace = service_call.data[mc.KEY_NAMESPACE]
            method = service_call.data.get(mc.KEY_METHOD, mc.METHOD_GET)
            key = service_call.data.get(mlc.CONF_KEY)
            if mc.KEY_PAYLOAD in service_call.data:
                payload = service_call.data[mc.KEY_PAYLOAD]
                if type(payload) is str:
                    try:
                        payload = json_loads(payload)
                    except Exception as e:
                        raise HomeAssistantError("Payload is not a valid JSON") from e
                elif type(payload) is not dict:
                    raise HomeAssistantError("Payload is not a valid dictionary")
            elif method == mc.METHOD_GET:
                try:
                    payload = mn.NAMESPACES[namespace].request_default[2]
                except Exception:  # whatever
                    payload = {}
            else:
                payload = {}  # likely failing the request...

            from_ = mlc.DOMAIN
            trigger_src = "service_request"

            async def _wrap_response(request: MerossRequest, coro):
                service_response["request"] = request
                try:
                    service_response["response"] = await coro(request)
                except Exception as exception:
                    service_response["exception"] = (
                        f"{exception.__class__.__name__}({str(exception)})"
                    )
                return service_response

            async def _async_bluetooth_request(bt_device: ComponentApi.BTDevice):
                return await _wrap_response(
                    MerossRequest(
                        namespace,
                        method,
                        payload,
                        "",
                        from_,
                        trigger_src,
                    ),
                    bt_device.async_request_raw,
                )

            async def _async_device_request(device: "Device"):
                return await _wrap_response(
                    MerossRequest(
                        namespace,
                        method,
                        payload,
                        device.key if key is None else key,
                        device._topic_response,
                        trigger_src,
                        device_id,
                    ),
                    (
                        device.async_mqtt_request_raw
                        if protocol == mlc.CONF_PROTOCOL_MQTT
                        else (
                            device.async_http_request_raw
                            if protocol == mlc.CONF_PROTOCOL_HTTP
                            else device.async_request_raw
                        )
                    ),
                )

            if device_id:
                if (
                    protocol in (mlc.CONF_PROTOCOL_AUTO, mlc.CONF_PROTOCOL_BLUETOOTH)
                ) and (_bt_device := self.get_bt_device(device_id)):
                    # check first since _async_device_request does not handle BT
                    return await _async_bluetooth_request(_bt_device)
                if device := self.devices.get(device_id):
                    return await _async_device_request(device)
                if (
                    protocol in (mlc.CONF_PROTOCOL_AUTO, mlc.CONF_PROTOCOL_MQTT)
                    and (mqtt_connection := self._mqtt_connection)
                    and mqtt_connection.mqtt_is_connected
                ):
                    service_response["request"] = request = MerossRequest(
                        namespace,
                        method,
                        payload,
                        self.key if key is None else key,
                        mqtt_connection.topic_response,
                        trigger_src,
                        device_id,
                    )
                    try:
                        service_response["response"] = (
                            await mqtt_connection.async_mqtt_request(request)
                        )
                    except Exception as exception:
                        service_response["exception"] = (
                            f"{exception.__class__.__name__}({str(exception)})"
                        )
                    return service_response

            if host:
                for device in self.active_devices():
                    if device.host == host:
                        return await _async_device_request(device)
                if (
                    protocol in (mlc.CONF_PROTOCOL_AUTO, mlc.CONF_PROTOCOL_BLUETOOTH)
                ) and (_bt_device := self._bt_devices.get(host)):
                    return await _async_bluetooth_request(_bt_device)

                if protocol in (mlc.CONF_PROTOCOL_AUTO, mlc.CONF_PROTOCOL_HTTP):
                    return await _wrap_response(
                        MerossRequest(
                            namespace,
                            method,
                            payload,
                            self.key if key is None else key,
                            from_,
                            trigger_src,
                        ),
                        MerossHttpClient(
                            host,
                            loop=hass.loop,
                            logger=self,
                        ).async_request_raw,
                    )

            raise HomeAssistantError(
                f"Unable to find a route to {device_id or host} using {protocol} protocol"
            )

        hass.services.async_register(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            _async_service_request,
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def _async_terminate(*args):
            """complete shutdown when HA exits. See self.async_shutdown for differences"""
            hass.services.async_remove(mlc.DOMAIN, mlc.SERVICE_REQUEST)
            for device in self.active_devices():
                await device.async_shutdown()
            for profile in self.active_profiles():
                await profile.async_shutdown()
            for bt_device in tuple(self._bt_devices.values()):
                await bt_device.async_shutdown()
            await mlq.MQTTProfile.async_shutdown(self)
            await MerossHttpClient.async_shutdown_session()
            self._mqtt_connection = None
            del self.device_registry  # type: ignore
            del self.entity_registry  # type: ignore
            del self.issue_registry  # type: ignore
            del self.hass  # type: ignore
            del self.api  # type: ignore
            hass.data.pop(mlc.DOMAIN)

        hass.bus.async_listen_once(hac.EVENT_HOMEASSISTANT_STOP, _async_terminate)
        # REMOVE
        if MEROSSDEBUG:

            import habluetooth

            habluetooth.wrappers._LOGGER.setLevel(ComponentApi.DEBUG)
            from bleak.backends.bluezdbus.client import logger as _bluez_logger

            _bluez_logger.setLevel(ComponentApi.DEBUG)

        hass.data[mlc.DOMAIN] = self

    # interface: ConfigEntryManager
    @override
    async def async_shutdown(self):
        # This is the base entry point when the config entry (MQTT Hub) is unloaded
        # but we want to actually preserve some of our state since ComponentApi provides
        # static services to the whole component and we want to preserve them even
        # when unloading the entry.
        # We're so trying to just destroy the config related state (entities for instance)
        # while preserving our mqtt_connection and device linking.
        # That's a risky mess
        # for real shutdown there's self.async_terminate
        await mlm.ConfigEntryManager.async_shutdown(self)

    @override
    def get_logger_name(self) -> str:
        return "api"

    # interface: MQTTProfile
    @property
    @override
    def allow_mqtt_publish(self):
        return True  # ComponentApi still doesnt support configuring entry for this

    @property
    @override
    def userid(self):
        return "0"

    @override
    def attach_mqtt(self, device: "Device"):
        self.mqtt_connection.attach(device)

    @override
    async def async_setup_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry"
    ):
        self.config_entry = config_entry  # type: ignore
        await self.entry_update_listener(hass, config_entry)
        await mlq.MQTTProfile.async_setup_entry(self, hass, config_entry)

    # interface: self
    @property
    def mqtt_connection(self):
        if not (mqtt_connection := self._mqtt_connection):
            self._mqtt_connection = mqtt_connection = HAMQTTConnection(self)
        return mqtt_connection

    async def async_build_device(
        self, device_id: str, config_entry: "ConfigEntry"
    ) -> "Device":
        """
        scans device descriptor to build a 'slightly' specialized Device
        The base Device class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        if device_id != config_entry.data[mlc.CONF_DEVICE_ID]:
            # shouldnt really happen: it means we have a 'critical' bug in our config entry/flow management
            # or that the config_entry was tampered
            raise ConfigEntryError(
                "Unrecoverable device id mismatch. 'ConfigEntry.unique_id' "
                "does not match the configured 'device_id'. "
                "Please delete the entry and reconfigure it"
            )
        descriptor = MerossDeviceDescriptor(config_entry.data[mlc.CONF_PAYLOAD])
        if device_id != descriptor.uuid:
            # this could happen (#341 raised the suspect) if a working device
            # 'suddenly' starts talking with another one and doesn't recognize
            # the mismatch (the issue appears as the device usually keeps updating
            # the config_entry data from live communication). This behavior is being
            # fixed in 4.5.0 so that devices don't update wrong configurations 'in the wild'
            raise ConfigEntryError(
                "Configuration data mismatch. Please refresh "
                "the configuration by hitting 'Configure' "
                "in the integration configuration page"
            )

        mixin_classes = []
        for key_digest in descriptor.digest:
            if key_digest not in MIXIN_DIGEST_INIT:
                continue
            _mixin_or_descriptor = MIXIN_DIGEST_INIT[key_digest]
            if isinstance(_mixin_or_descriptor, tuple):
                with self.exception_warning(
                    "initializing digest(%s) mixin", key_digest
                ):
                    _mixin_or_descriptor = getattr(
                        await self.async_import_module(_mixin_or_descriptor[0]),
                        _mixin_or_descriptor[1],
                    )
                    MIXIN_DIGEST_INIT[key_digest] = _mixin_or_descriptor
                    mixin_classes.append(_mixin_or_descriptor)
            else:
                mixin_classes.append(_mixin_or_descriptor)

        # We must be careful when ordering the mixin and leave Device as last class.
        # Messing up with that will cause MRO to not resolve inheritance correctly.
        # see https://github.com/albertogeniola/MerossIot/blob/0.4.X.X/meross_iot/device_factory.py
        mixin_classes.append(mld.Device)
        # build a label to cache the set
        class_name = ""
        for m in mixin_classes:
            class_name = class_name + m.__name__
        try:
            return self._deviceclasses[class_name](self, config_entry, descriptor)
        except KeyError as key_error:
            if key_error.args[0] != class_name:
                raise
            class_type = type(class_name, tuple(mixin_classes), {})
            self._deviceclasses[class_name] = class_type
            return class_type(self, config_entry, descriptor)

    async def async_available_timezones(self):
        timezones = self._available_timezones
        if not timezones:

            def _load():
                """
                These functions will use low levels imports and HA core 2024.5
                complains about executing it in the main loop thread. We'll
                so run these in an executor
                """
                return sorted(zoneinfo.available_timezones())

            try:
                timezones = await self.hass.async_add_executor_job(_load)
            except Exception as e:
                self.log_exception(self.WARNING, e, "retrieving available timezones")
                timezones = []
            self._available_timezones = timezones

        return timezones

    async def async_load_zoneinfo(self, key: str):
        """
        Creates a ZoneInfo instance from an executor.
        HA core 2024.5 might complain if ZoneInfo needs to load files (no cache hit)
        so we have to always demand this to an executor because the 'decision' to
        load is embedded inside the ZoneInfo initialization.
        A bit cumbersome though..
        """
        try:
            return self._zoneinfo[key]
        except KeyError:
            self._zoneinfo[key] = tz = await self.hass.async_add_executor_job(
                zoneinfo.ZoneInfo,
                key,
            )
            return tz

    async def async_import_module(self, name: str):
        try:
            return self._import_module_cache[name]
        except KeyError:
            async with self._import_module_lock:
                # check (again) the module was not asyncronously loaded when waiting the lock
                try:
                    return self._import_module_cache[name]
                except KeyError:
                    module = await self.hass.async_add_executor_job(
                        importlib.import_module,
                        name,
                        "custom_components.meross_lan",
                    )
                    self._import_module_cache[name] = module
                    return module

    def get_config_entry(self, unique_id: str):
        """Gets the configured entry if it exists."""
        try:
            return self.hass.config_entries.async_entry_for_domain_unique_id(
                mlc.DOMAIN, unique_id
            )
        except AttributeError:
            for config_entry in self.hass.config_entries.async_entries(mlc.DOMAIN):
                if config_entry.unique_id == unique_id:
                    return config_entry
            return None

    def get_config_flow(self, unique_id: str):
        """Returns the current flow (in progres) if any."""
        for progress in self.hass.config_entries.flow.async_progress_by_handler(
            mlc.DOMAIN,
            include_uninitialized=True,
            match_context={"unique_id": unique_id},
        ):
            return progress
        return None

    def schedule_entry_reload(self, entry_id: str):
        """Reloads an entry. Due to the nature of hass tasks api this could be
        eagerly executed (or not)."""
        try:
            # should work straight...
            self.hass.config_entries.async_schedule_reload(entry_id)
        except AttributeError:
            """Pre HA core 2024.2 compatibility layer"""
            config_entries = self.hass.config_entries
            if entry := config_entries.async_get_entry(entry_id):
                entry.async_cancel_retry_setup()
                self.async_create_task(
                    config_entries.async_reload(entry_id),
                    f".schedule_reload({entry.title},{entry_id})",
                )

    def get_bt_device(self, uuid: str):
        for bt_device in self._bt_devices.values():
            if bt_device.uuid == uuid:
                return bt_device
        return None

    async def async_bt_advertisement(
        self, service_info: ha_bt.BluetoothServiceInfoBleak, flow: "ConfigFlow", /
    ):
        bt_address = service_info.address
        self.log(
            self.DEBUG,
            "Received BT advertisement (address: %s service_info: %r)",
            bt_address,
            service_info,
        )

        try:
            self._bt_devices[bt_address].info = service_info
            self.log(
                self.DEBUG,
                "Updated BTDevice(%s)",
                bt_address,
            )
            raise AbortFlow("already_configured")

        except KeyError:
            # First time seen: proceed to identification
            """
            try:
                uuid = service_info.manufacturer_data[0xFFFF].hex()
                if not mc.RE_PATTERN_UUID.match(uuid):
                    uuid = None
                    self.log(
                        ComponentApi.DEBUG,
                        "Malformed UUID in manufacturer data: %s",
                        uuid,
                    )
            except KeyError:
                uuid = None
                self.log(
                    ComponentApi.DEBUG,
                    "Missing UUID in manufacturer data: %r",
                    service_info.manufacturer_data,
                )
            """
            bt_device = ComponentApi.BTDevice(self, bt_address, flow.flow_id)
            bt_device.info = service_info
            return await bt_device._init_task
