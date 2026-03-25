import asyncio
from typing import TYPE_CHECKING, override

from bleak.exc import BleakError
from homeassistant.components import bluetooth as ha_bt, mqtt
from homeassistant.core import SupportsResponse, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

# import core modules instead of symbols to ease patching in a single place
from . import ConfigEntryType, mqtt_profile as mlq
from .. import const as mlc
from ..merossclient import HostAddress
from ..merossclient.client import Transport, bluetooth as m_bt
from ..merossclient.client.http import HttpClient
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import (
    MerossAckReply,
    MerossPushReply,
    MerossRequest,
    json_loads,
)
from .manager import ConfigEntryManager

if TYPE_CHECKING:

    from typing import Callable, Final, Literal, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import (
        CALLBACK_TYPE,
        HomeAssistant,
        ServiceCall,
        ServiceResponse,
    )

    from ..config_flow import ConfigFlow
    from ..merossclient import DeviceDescriptor
    from ..merossclient.protocol.message import MerossMessage
    from .device import Device
    from .meross_profile import MerossProfile


class HAMQTTConnection(mlq.MQTTConnection):

    if TYPE_CHECKING:

        class ConnectArgs(mlq.MQTTConnection.ConnectArgs):
            pass

        parent: Final["ComponentApi"]  # type: ignore[override]

        _mqtt_subscribe_unsub: Callable | None
        _mqtt_disconnected_unsub: Callable | None
        _mqtt_connected_unsub: Callable | None
        _mqtt_subscribe_future: asyncio.Future[bool] | None

    __slots__ = (
        "_mqtt_subscribe_unsub",
        "_mqtt_disconnected_unsub",
        "_mqtt_connected_unsub",
        "_mqtt_subscribe_future",
    )

    def __init__(self, api: "ComponentApi", /):
        mlq.MQTTConnection.__init__(
            self,
            HostAddress("homeassistant", 0),
            api,
            from_=mc.TOPIC_RESPONSE.format(mlc.DOMAIN),
            loop=api.loop,
        )
        self._mqtt_subscribe_unsub = None
        self._mqtt_disconnected_unsub = None
        self._mqtt_connected_unsub = None
        self._mqtt_subscribe_future = None

    @override  # MQTTConnection
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
        if self._mqtt_subscribe_unsub:
            return True

        if self._mqtt_subscribe_future:
            return await self._mqtt_subscribe_future

        hass = self.parent.hass
        self._mqtt_subscribe_future = hass.loop.create_future()
        try:
            self._mqtt_subscribe_unsub = await mqtt.async_subscribe(
                hass, mc.TOPIC_DISCOVERY, self.on_message
            )

            @callback
            def _connection_status_callback(connected: bool):
                if connected:
                    self.on_connect()
                else:
                    self.on_disconnect()

            try:
                # HA core 2024.6
                self._mqtt_connected_unsub = mqtt.async_subscribe_connection_status(
                    hass, _connection_status_callback
                )
            except:
                self._mqtt_disconnected_unsub = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_DISCONNECTED, self.on_disconnect  # type: ignore (removed in HA core 2024.6)
                )
                self._mqtt_connected_unsub = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_CONNECTED, self.on_connect  # type: ignore (removed in HA core 2024.6)
                )
            if mqtt.is_connected(hass):
                self.on_connect()
        except Exception as exception:
            self.log_exception(self.WARNING, exception, "async_connect", timeout=14400)
        finally:
            self._mqtt_subscribe_future.set_result(
                self._mqtt_subscribe_unsub is not None
            )
            self._mqtt_subscribe_future = None

        return self._mqtt_subscribe_unsub is not None

    @override  # MQTTConnection
    async def async_disconnect(self, /):
        if self._mqtt_subscribe_future:
            await self._mqtt_subscribe_future
        if self._mqtt_connected_unsub:
            self._mqtt_connected_unsub()
            self._mqtt_connected_unsub = None
        if self._mqtt_disconnected_unsub:
            self._mqtt_disconnected_unsub()
            self._mqtt_disconnected_unsub = None
        if self._mqtt_subscribe_unsub:
            self._mqtt_subscribe_unsub()
            self._mqtt_subscribe_unsub = None
        if self.is_connected:
            self.on_disconnect()

    @override  # MQTTConnection
    async def async_publish_raw(
        self,
        message: "MerossMessage",
        /,
        **kwargs: "Unpack[HAMQTTConnection.RequestRawArgs]",
    ):
        self.on_tx(message)
        try:
            await mqtt.async_publish(
                self.parent.hass,
                mc.TOPIC_REQUEST.format(kwargs["uuid"]),
                message.json,
            )
            self.on_publish()
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "async_publish_raw %s %s (messageId:%s uuid:%s)",
                message.method,
                message.namespace,
                message.messageid,
                uuid=kwargs["uuid"],
                timeout=14400,
            )
            raise

    # interface: self
    @property
    def mqtt_is_subscribed(self):
        return self._mqtt_subscribe_unsub is not None

    @callback
    def on_connect(self, /):
        """called when the underlying mqtt.Client connects to the broker"""
        # try to get the HA broker host address
        with self.exception_warning("on_connect: recovering broker conf"):

            mqtt_data = self.parent.hass.data[mqtt.DATA_MQTT]
            if mqtt_data and mqtt_data.client:
                conf = mqtt_data.client.conf
                self.id.host = conf[mqtt.CONF_BROKER]
                self.id.port = conf.get(mlc.hac.CONF_PORT, mqtt.const.DEFAULT_PORT)
                self.configure_logger()

        super().on_connect()

    # these handlers are used to manage session establishment on MQTT.
    # They are typically sent by the device when they connect to the broker
    # and they are used to mimic the official Meross brokers session managment
    # They're implemented at the MQTTConnection level since the device might not be
    # configured yet in meross_lan. When the device is configured, we still manage
    # these 'session messages' here but we'll forward them to the device too in order
    # to trigger all of the device connection management.
    def _handle_Appliance_Control_Bind(self, message: "MerossMessage", /):
        # this transaction appears when a device (firstly)
        # connects to an MQTT broker and tries to 'register'
        # itself. Our guess right now is to just SETACK
        # trying fix #346. When building the reply, the
        # meross broker sets the from field as
        # "from": "cloud/sub/kIGFRwvtAQP4sbXv/58c35d719350a689"
        # and the fields look like hashes or something since
        # they change between attempts (hashed broker id ?)
        # At any rate I don't have a clue on how to properly
        # replicate this and the "from" field is set as usual

        uuid = message.uuid
        try:
            if device := self.parent.devices[uuid]:
                key = device.key
            else:  # device not loaded...
                device_config_entry = self.parent.get_config_entry(uuid)
                if device_config_entry:
                    key = device_config_entry.data.get(mlc.CONF_KEY) or ""
                else:
                    key = self.parent.key
        except KeyError:  # device not configured
            key = self.parent.key
        if message.method == mc.METHOD_SET:
            self.create_task(
                self.async_publish_raw(
                    MerossAckReply(
                        message,
                        {},
                        key,
                        mc.TOPIC_RESPONSE.format(uuid),
                    ),
                    uuid=uuid,
                ),
                "._handle_Appliance_Control_Bind",
                eager_start=True,
            )
        # keep forwarding the message
        return False

    def _handle_Appliance_Control_ConsumptionConfig(self, message: "MerossMessage", /):
        # this message is published by mss switches
        # and it appears newer mss315 could abort their connection
        # if not replied (see #346)
        if message.method == mc.METHOD_PUSH:
            self.create_task(
                self.async_publish_raw(
                    MerossPushReply(message, message.payload),
                    uuid=message.uuid,
                ),
                "._handle_Appliance_Control_ConsumptionConfig",
                eager_start=True,
            )
        # keep forwarding the message
        return False

    def _handle_Appliance_System_Clock(self, message: "MerossMessage", /):
        # this is part of initial flow over MQTT
        # we'll try to set the correct time in order to avoid
        # having NTP opened to setup the device
        # Note: I actually see this NS only on mss310 plugs
        # (msl120j bulb doesnt have it)
        if message.method == mc.METHOD_PUSH:
            self.create_task(
                self.async_publish_raw(
                    MerossPushReply(
                        message, {mc.KEY_CLOCK: {mc.KEY_TIMESTAMP: int(self.time())}}
                    ),
                    uuid=message.uuid,
                ),
                "._handle_Appliance_System_Clock",
                eager_start=True,
            )
        # keep forwarding the message
        return False


HAMQTTConnection.SESSION_HANDLERS = {
    mn.Appliance_Control_Bind: HAMQTTConnection._handle_Appliance_Control_Bind,
    mn.Appliance_Control_ConsumptionConfig: HAMQTTConnection._handle_Appliance_Control_ConsumptionConfig,
    mn.Appliance_System_Clock: HAMQTTConnection._handle_Appliance_System_Clock,
} | mlq.MQTTConnection.SESSION_HANDLERS  # type: ignore


class ComponentApi(mlq.MQTTProfile):
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """

    class BTClient(m_bt.BluetoothClient):
        if TYPE_CHECKING:
            parent: "ComponentApi | Device"  # type: ignore[override]
            api: Final["ComponentApi"]  # type: ignore[override]
            descriptor: Final[DeviceDescriptor]  # type: ignore[override]
            address: Final[str]
            info: ha_bt.BluetoothServiceInfoBleak
            uuid: Final[str]  # BEWARE: not valid until _init_task done
            _flow_id: Final[str]
            _bt_unavailable_unsub: Final[CALLBACK_TYPE]
            _init_task: asyncio.Task["ComponentApi.BTClient"]

        __slots__ = (
            "address",
            "info",
            "uuid",
            "_flow_id",
            "_bt_unavailable_unsub",
            "_init_task",
        )

        def __init__(self, api: "ComponentApi", address: str, flow_id: str, /):
            self.api = api
            self.address = address
            self._flow_id = flow_id
            m_bt.BluetoothClient.__init__(
                self, address, api, from_=mlc.DOMAIN, loop=api.loop
            )
            self._bt_unavailable_unsub = ha_bt.async_track_unavailable(
                api.hass, self._bt_unavailable, address, connectable=True
            )
            api._bt_devices[address] = self
            self._init_task = self.create_task(
                self._async_init(), f"BTDevice({address}).__init__"
            )

        async def _async_init(self):
            api = self.api
            while True:
                try:
                    self.descriptor = await self.async_identify()  # type: ignore
                    self.uuid = uuid = self.descriptor.uuid  # type: ignore
                    for _bt_device in api._bt_devices.values():
                        if (_bt_device is not self) and (_bt_device.uuid == uuid):
                            # an existing device has a new bt address
                            await _bt_device.async_shutdown()
                            break
                    # check to see if the device is one of our configureds
                    try:
                        device = api.devices[uuid]
                        if device:
                            conf_transport = device.config.get(mlc.CONF_PROTOCOL)
                        else:
                            config_entry = api.get_config_entry(uuid)
                            assert config_entry
                            conf_transport = config_entry.data.get(mlc.CONF_PROTOCOL)
                        if conf_transport == self.TRANSPORT:
                            # already configured to use BT
                            if device:
                                device.add_client(self)
                            raise AbortFlow("already_configured")
                    except KeyError:
                        # device not configured yet..proceed with ConfigFlow
                        pass

                    return self
                except (TimeoutError, BleakError) as e:
                    self.log_exception(
                        self.WARNING,
                        e,
                        "device identification. Retrying in 30 sec",
                    )
                    await self.async_disconnect()
                    await asyncio.sleep(30)

        async def async_shutdown(self):
            self._bt_unavailable_unsub()
            if self._init_task.cancel():
                try:
                    await self._init_task
                except asyncio.CancelledError:
                    pass
            await super().async_shutdown()
            del self.api._bt_devices[self.address]

        @override
        def on_device_add(self, device: "Device"):
            # we're using a 'dirty' fix to redirect bt logs to the device itself
            self.parent = device
            super().on_device_add(device)

        @override
        def on_device_remove(self, device: "Device"):
            super().on_device_remove(device)
            self.parent = self.api

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

        @callback
        def _bt_unavailable(self, info: ha_bt.BluetoothServiceInfoBleak):
            self.log(self.DEBUG, "_bt_unavailable(info: %s)", info)
            self.create_task(self.async_shutdown(), "_bt_unavailable", eager_start=True)

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
        a ConfigEntryManager that we don't want to persist to hass storage (useless overhead)
        since they're just runtime context but we need an independent storage than
        ConfigEntryManager since these info are needed during async_setup_entry.
        See the tracing feature activated through the OptionsFlow for insights.
        """

        device_registry: Final[dr.DeviceRegistry]
        entity_registry: Final[er.EntityRegistry]
        issue_registry: Final[ir.IssueRegistry]
        config_entries: Final
        flow_manager: Final

        _mqtt_connection: HAMQTTConnection | None

        _bt_devices: Final[dict[str, BTClient]]

        # Overrides

    __slots__ = (
        "hass",
        "devices",
        "profiles",
        "managers_transient_state",
        "device_registry",
        "entity_registry",
        "issue_registry",
        "config_entries",
        "flow_manager",
        "_mqtt_connection",
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
        self.config_entries = hass.config_entries
        self.flow_manager = hass.config_entries.flow
        self._mqtt_connection = None
        for config_entry in self.config_entries.async_entries(mlc.DOMAIN):
            match ConfigEntryType.get_type_and_id(config_entry.unique_id):
                case (ConfigEntryType.DEVICE, device_id):
                    self.devices[device_id] = None
                case (ConfigEntryType.PROFILE, profile_id):
                    self.profiles[profile_id] = None
        self._bt_devices = {}
        self.api = self  # type: ignore
        mlq.MQTTProfile.__init__(
            self,
            mlc.CONF_PROFILE_ID_LOCAL,
            self,
            self.config_entries.async_entry_for_domain_unique_id(
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
            try:
                protocol = Transport.from_str(service_call.data[mlc.CONF_PROTOCOL])
            except KeyError:
                protocol = Transport.AUTO
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

            async def _async_bluetooth_request(bt_device: ComponentApi.BTClient):
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
                _client = device._clients.get(protocol) or device._clients.get(
                    device.transport
                )
                if not _client:
                    raise HomeAssistantError(
                        f"Device {device.display_name} does not currently provide {protocol} connectivity"
                    )
                return await _wrap_response(
                    MerossRequest(
                        namespace,
                        method,
                        payload,
                        _client.key if key is None else key,
                        _client.from_,
                        trigger_src,
                    ),
                    _client.async_request_raw,
                )

            if device_id:
                if device := self.devices.get(device_id):
                    return await _async_device_request(device)
                if (protocol in (Transport.AUTO, Transport.BLUETOOTH)) and (
                    _bt_device := self.get_bt_client(device_id)
                ):
                    return await _async_bluetooth_request(_bt_device)
                if (
                    protocol in (Transport.AUTO, Transport.MQTT)
                    and (mqtt_connection := self._mqtt_connection)
                    and mqtt_connection.is_connected
                ):
                    service_response["request"] = request = MerossRequest(
                        namespace,
                        method,
                        payload,
                        self.key if key is None else key,
                        mqtt_connection.from_,
                        trigger_src,
                    )
                    try:
                        service_response["response"] = (
                            await mqtt_connection.async_request_raw(
                                request, uuid=device_id
                            )
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
                if (protocol in (Transport.AUTO, Transport.BLUETOOTH)) and (
                    _bt_device := self._bt_devices.get(host)
                ):
                    return await _async_bluetooth_request(_bt_device)
                if protocol in (Transport.AUTO, Transport.HTTP):
                    return await _wrap_response(
                        MerossRequest(
                            namespace,
                            method,
                            payload,
                            self.key if key is None else key,
                            from_,
                            trigger_src,
                        ),
                        HttpClient(
                            host,
                            self,
                            from_=mlc.DOMAIN,
                            trigger_src=trigger_src,
                            loop=hass.loop,
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
            await HttpClient.async_shutdown_session()
            self._mqtt_connection = None
            del self.device_registry  # type: ignore
            del self.entity_registry  # type: ignore
            del self.issue_registry  # type: ignore
            del self.config_entries  # type: ignore
            del self.flow_manager  # type: ignore
            del self.hass  # type: ignore
            del self.api  # type: ignore
            hass.data.pop(mlc.DOMAIN)

        hass.bus.async_listen_once(mlc.hac.EVENT_HOMEASSISTANT_STOP, _async_terminate)
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
        await ConfigEntryManager.async_shutdown(self)

    @override
    def get_logger_name(self) -> str:
        return "api"

    @override
    async def async_setup_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry"
    ):
        self.config_entry = config_entry  # type: ignore
        config = self.config = config_entry.data
        self.key = config.get(mlc.CONF_KEY) or ""
        self.obfuscate = config.get(mlc.CONF_OBFUSCATE, True)
        self.configure_logger()
        await mlq.MQTTProfile.async_setup_entry(self, hass, config_entry)
        self.mqtt_connection.entry_update_listener(self)

    # interface: MQTTProfile
    @property
    @override
    def is_cloud_profile(self) -> bool:
        return False

    @property
    @override
    def allow_mqtt_publish(self):
        return True  # ComponentApi still doesnt support configuring entry for this

    @property
    @override
    def userid(self):
        return "0"

    @override
    def get_connection(self, device: "Device"):
        return self.mqtt_connection

    # interface: self
    @property
    def mqtt_connection(self):
        if not (mqtt_connection := self._mqtt_connection):
            self._mqtt_connection = mqtt_connection = HAMQTTConnection(self)
        return mqtt_connection

    def get_config_entry(self, unique_id: str):
        """Gets the configured entry if it exists."""
        try:
            return self.config_entries.async_entry_for_domain_unique_id(
                mlc.DOMAIN, unique_id
            )
        except AttributeError:
            for config_entry in self.config_entries.async_entries(mlc.DOMAIN):
                if config_entry.unique_id == unique_id:
                    return config_entry
            return None

    def get_config_flow(self, unique_id: str):
        """Returns the current flow (in progres) if any."""
        for progress in self.flow_manager.async_progress_by_handler(
            mlc.DOMAIN,
            include_uninitialized=True,
            match_context={"unique_id": unique_id},
        ):
            return progress
        return None

    def get_bt_client(self, uuid: str):
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
            bt_device = ComponentApi.BTClient(self, bt_address, flow.flow_id)
            bt_device.info = service_info
            return await bt_device._init_task
