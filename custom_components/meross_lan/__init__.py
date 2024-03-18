"""The Meross IoT local LAN integration."""

from __future__ import annotations

from importlib import import_module
from time import time
import typing

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const as mlc
from .helpers import (
    LOGGER,
    ConfigEntriesHelper,
    ConfigEntryType,
    Loggable,
    schedule_async_callback,
)
from .helpers.manager import ApiProfile, ConfigEntryManager
from .meross_device import MerossDevice
from .meross_profile import MerossCloudProfile, MerossCloudProfileStore, MQTTConnection
from .merossclient import (
    MEROSSDEBUG,
    HostAddress,
    MerossAckReply,
    MerossDeviceDescriptor,
    MerossPushReply,
    MerossRequest,
    cloudapi,
    const as mc,
    get_default_payload,
    json_loads,
)
from .merossclient.httpclient import MerossHttpClient

if typing.TYPE_CHECKING:
    from typing import Callable

    from homeassistant.components.mqtt import async_publish as mqtt_async_publish
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import ServiceCall, ServiceResponse

    from .merossclient import MerossHeaderType, MerossMessage, MerossPayloadType
    from .merossclient.cloudapi import MerossCloudCredentials


else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


DIGEST_INITIALIZERS = {
    mc.KEY_DIFFUSER: (".devices.mod100", "DiffuserMixin"),
    mc.KEY_FAN: (".fan", "FanMixin"),
    mc.KEY_GARAGEDOOR: (".cover", "GarageMixin"),
    mc.KEY_LIGHT: (".light", "LightMixin"),
    mc.KEY_THERMOSTAT: (".devices.thermostat", "ThermostatMixin"),
    mc.KEY_SPRAY: (".select", "SprayMixin"),
}

MerossDevice.ENTITY_INITIALIZERS = {
    mc.NS_APPLIANCE_CONFIG_OVERTEMP: (".devices.mss", "OverTempEnableSwitch"),
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG: (
        ".devices.mss",
        "ConsumptionConfigNamespaceHandler",
    ),
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX: (".devices.mss", "ConsumptionXSensor"),
    mc.NS_APPLIANCE_CONTROL_ELECTRICITY: (
        ".devices.mss",
        "ElectricityNamespaceHandler",
    ),
    mc.NS_APPLIANCE_CONTROL_FAN: (".fan", "FanNamespaceHandler"),
    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE: (
        ".sensor",
        "FilterMaintenanceNamespaceHandler",
    ),
    mc.NS_APPLIANCE_CONTROL_MP3: (".media_player", "MLMp3Player"),
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK: (".switch", "PhysicalLockSwitch"),
    mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS: (
        ".devices.screenbrightness",
        "ScreenBrightnessNamespaceHandler",
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: (".cover", "MLRollerShutter"),
    mc.NS_APPLIANCE_SYSTEM_DNDMODE: (".light", "MLDNDLightEntity"),
    mc.NS_APPLIANCE_SYSTEM_RUNTIME: (".sensor", "MLSignalStrengthSensor"),
}


class HAMQTTConnection(MQTTConnection):
    __slots__ = (
        "_unsub_mqtt_subscribe",
        "_unsub_mqtt_disconnected",
        "_unsub_mqtt_connected",
        "_mqtt_subscribing",
        "_unsub_random_disconnect",
    )

    def __init__(self, api: MerossApi):
        super().__init__(
            api,
            HostAddress("homeassistant", 0),
            mc.TOPIC_RESPONSE.format(mlc.DOMAIN),
        )
        self._unsub_mqtt_subscribe: Callable | None = None
        self._unsub_mqtt_disconnected: Callable | None = None
        self._unsub_mqtt_connected: Callable | None = None
        self._mqtt_subscribing = False  # guard for asynchronous mqtt sub registration
        if MEROSSDEBUG:
            # TODO : check bug in hass shutdown
            async def _async_random_disconnect():
                self._unsub_random_disconnect = schedule_async_callback(
                    MerossApi.hass, 60, _async_random_disconnect
                )
                if self._mqtt_subscribing:
                    return
                elif self._unsub_mqtt_subscribe is None:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(self.DEBUG, "random connect")
                        await self.async_mqtt_subscribe()
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(self.DEBUG, "random disconnect")
                        await self.async_mqtt_unsubscribe()

            self._unsub_random_disconnect = schedule_async_callback(
                MerossApi.hass, 60, _async_random_disconnect
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

    @property
    def is_cloud_connection(self):
        return False

    async def _async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> tuple[str, int]:
        await mqtt_async_publish(
            MerossApi.hass, mc.TOPIC_REQUEST.format(device_id), request.json()
        )
        self._mqtt_published()
        return self._MQTT_PUBLISH, self.DEFAULT_RESPONSE_TIMEOUT

    # interface: self
    @property
    def mqtt_is_subscribed(self):
        return self._unsub_mqtt_subscribe is not None

    async def async_mqtt_subscribe(self):
        if not (self._mqtt_subscribing or self._unsub_mqtt_subscribe):
            # dumb re-entrant code protection
            self._mqtt_subscribing = True
            with self.exception_warning("async_mqtt_subscribe"):
                from homeassistant.components import mqtt

                global mqtt_async_publish
                mqtt_async_publish = mqtt.async_publish
                hass = MerossApi.hass
                self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                    hass, mc.TOPIC_DISCOVERY, self.async_mqtt_message
                )
                self._unsub_mqtt_disconnected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_DISCONNECTED, self._mqtt_disconnected
                )
                self._unsub_mqtt_connected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_CONNECTED, self._mqtt_connected
                )
                if mqtt.is_connected(hass):
                    self._mqtt_connected()
            self._mqtt_subscribing = False

        return self._unsub_mqtt_subscribe is not None

    async def async_mqtt_unsubscribe(self):
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

            mqtt_data = mqtt.get_mqtt_data(MerossApi.hass)
            if mqtt_data and mqtt_data.client:
                conf = mqtt_data.client.conf
                self.broker.host = conf[mqtt.CONF_BROKER]
                self.broker.port = conf.get(mqtt.CONF_PORT, mqtt.const.DEFAULT_PORT)
                self.configure_logger()

        super()._mqtt_connected()

    # these handlers are used to manage session establishment on MQTT.
    # They are typically sent by the device when they connect to the broker
    # and they are used to mimic the official Meross brokers session managment
    # They're implemented at the MQTTConnection level since the device might not be
    # configured yet in meross_lan. When the device is configured, we still manage
    # these 'session messages' here but we'll forward them to the device too in order
    # to trigger all of the device connection management.
    async def _handle_Appliance_Control_Bind(
        self: MQTTConnection,
        device_id: str,
        header: MerossHeaderType,
        payload: MerossPayloadType,
    ):
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

        if device_id in MerossApi.devices:
            if device := MerossApi.devices[device_id]:
                key = device.key
            else:  # device not loaded...
                helper = ConfigEntriesHelper(MerossApi.hass)
                device_entry = helper.get_config_entry(device_id)
                if device_entry:
                    key = device_entry.data.get(mlc.CONF_KEY) or ""
                else:
                    key = self.profile.key
        else:
            key = self.profile.key
        if header[mc.KEY_METHOD] == mc.METHOD_SET:
            await self.async_mqtt_publish(
                device_id,
                MerossAckReply(
                    key,
                    header,
                    {},
                    mc.TOPIC_RESPONSE.format(device_id),
                ),
            )
        # keep forwarding the message
        return False

    async def _handle_Appliance_Control_ConsumptionConfig(
        self: MQTTConnection,
        device_id: str,
        header: MerossHeaderType,
        payload: MerossPayloadType,
    ):
        # this message is published by mss switches
        # and it appears newer mss315 could abort their connection
        # if not replied (see #346)
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            await self.async_mqtt_publish(
                device_id,
                MerossPushReply(header, payload),
            )
        # keep forwarding the message
        return False

    async def _handle_Appliance_System_Clock(
        self: MQTTConnection,
        device_id: str,
        header: MerossHeaderType,
        payload: MerossPayloadType,
    ):
        # this is part of initial flow over MQTT
        # we'll try to set the correct time in order to avoid
        # having NTP opened to setup the device
        # Note: I actually see this NS only on mss310 plugs
        # (msl120j bulb doesnt have it)
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            await self.async_mqtt_publish(
                device_id,
                MerossPushReply(
                    header,
                    {mc.KEY_CLOCK: {mc.KEY_TIMESTAMP: int(time())}},
                ),
            )
        # keep forwarding the message
        return False


HAMQTTConnection.SESSION_HANDLERS = {
    mc.NS_APPLIANCE_CONTROL_BIND: HAMQTTConnection._handle_Appliance_Control_Bind,
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG: HAMQTTConnection._handle_Appliance_Control_ConsumptionConfig,
    mc.NS_APPLIANCE_SYSTEM_CLOCK: HAMQTTConnection._handle_Appliance_System_Clock,
    mc.NS_APPLIANCE_SYSTEM_ONLINE: MQTTConnection._handle_Appliance_System_Online,
}


class MerossApi(ApiProfile):
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """

    __slots__ = (
        "_deviceclasses",
        "_mqtt_connection",
    )

    @staticmethod
    def get(hass: HomeAssistant) -> MerossApi:
        """
        Set up the MerossApi component.
        'Our' truth singleton is saved in hass.data[DOMAIN] and
        MerossApi.api is just a cache to speed access
        """
        if not (api := Loggable.api):
            Loggable.api = hass.data[mlc.DOMAIN] = api = MerossApi(hass)
            Loggable.hass = hass

            async def _async_unload_merossapi(_event) -> None:
                await api.async_terminate()
                Loggable.api = None  # type: ignore
                Loggable.hass = None  # type: ignore
                hass.data.pop(mlc.DOMAIN)

            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, _async_unload_merossapi
            )
        return api

    def __init__(self, hass: HomeAssistant):
        super().__init__(mlc.CONF_PROFILE_ID_LOCAL, None)
        self._deviceclasses: dict[str, type] = {}
        self._mqtt_connection: HAMQTTConnection | None = None

        for config_entry in hass.config_entries.async_entries(mlc.DOMAIN):
            match ConfigEntryType.get_type_and_id(config_entry.unique_id):
                case (ConfigEntryType.DEVICE, device_id):
                    self.devices[device_id] = None
                case (ConfigEntryType.PROFILE, profile_id):
                    self.profiles[profile_id] = None

        async def _async_service_request(service_call: ServiceCall) -> ServiceResponse:
            service_response = {}
            device_id = service_call.data.get(mlc.CONF_DEVICE_ID)
            host = service_call.data.get(mlc.CONF_HOST)
            if not device_id and not host:
                raise HomeAssistantError(
                    "Missing both device_id and host: provide at least one valid entry"
                )
            protocol = mlc.CONF_PROTOCOL_OPTIONS.get(
                service_call.data.get(mlc.CONF_PROTOCOL), mlc.CONF_PROTOCOL_AUTO
            )
            namespace = service_call.data[mc.KEY_NAMESPACE]
            method = service_call.data.get(mc.KEY_METHOD, mc.METHOD_GET)
            key = service_call.data.get(mlc.CONF_KEY)
            if mc.KEY_PAYLOAD in service_call.data:
                try:
                    payload = json_loads(service_call.data[mc.KEY_PAYLOAD])
                except Exception as e:
                    raise HomeAssistantError("Payload is not a valid JSON") from e
            elif method == mc.METHOD_GET:
                payload = get_default_payload(namespace)
            else:
                payload = {}  # likely failing the request...

            async def _async_device_request(device: MerossDevice):
                service_response["request"] = request = MerossRequest(
                    key or device.key,
                    namespace,
                    method,
                    payload,
                    device._topic_response,
                )
                service_response["response"] = (
                    await device.async_mqtt_request_raw(request)
                    if protocol is mlc.CONF_PROTOCOL_MQTT
                    else (
                        await device.async_http_request_raw(request)
                        if protocol is mlc.CONF_PROTOCOL_HTTP
                        else await device.async_request_raw(request)
                    )
                ) or {}
                return service_response

            if device_id:
                if device := self.devices.get(device_id):
                    return await _async_device_request(device)
                if (
                    protocol is not mlc.CONF_PROTOCOL_HTTP
                    and (mqtt_connection := self._mqtt_connection)
                    and mqtt_connection.mqtt_is_connected
                ):
                    service_response["request"] = request = MerossRequest(
                        key or self.key,
                        namespace,
                        method,
                        payload,
                        mqtt_connection.topic_response,
                    )
                    service_response["response"] = (
                        await mqtt_connection.async_mqtt_publish(device_id, request)
                        or {}
                    )
                    return service_response

            if host:
                for device in self.active_devices():
                    if device.host == host:
                        return await _async_device_request(device)

                if protocol is not mlc.CONF_PROTOCOL_MQTT:
                    service_response["request"] = request = MerossRequest(
                        key or self.key,
                        namespace,
                        method,
                        payload,
                        mc.MANUFACTURER,
                    )
                    try:
                        service_response["response"] = (
                            await MerossHttpClient(
                                host,
                                key or self.key,
                                None,
                                self,  # type: ignore (self almost duck-compatible with logging.Logger)
                                self.VERBOSE,
                            ).async_request_raw(request.json())
                            or {}
                        )
                    except Exception as exception:
                        service_response["exception"] = (
                            f"{exception.__class__.__name__}({str(exception)})"
                        )

                    return service_response

            raise HomeAssistantError(
                f"Unable to find a route to {device_id or host} using {protocol} protocol"
            )

        hass.services.async_register(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            _async_service_request,
            supports_response=SupportsResponse.OPTIONAL,
        )
        return

    # interface: ConfigEntryManager
    async def async_shutdown(self):
        # This is the base entry point when the config entry (MQTT Hub) is unloaded
        # but we want to actually preserve some of our state since MerossApi provides
        # static services to the whole component and we want to preserve them even
        # when unloading the entry.
        # We're so trying to just destroy the config related state (entities for instance)
        # while preserving our mqtt_connection and device linking.
        # That's a risky mess
        # for real shutdown there's self.async_terminate
        await ConfigEntryManager.async_shutdown(self)

    def get_logger_name(self) -> str:
        return "api"

    # interface: ApiProfile
    @property
    def allow_mqtt_publish(self):
        return True  # MerossApi still doesnt support configuring entry for this

    def attach_mqtt(self, device: MerossDevice):
        self.mqtt_connection.attach(device)

    # interface: self
    async def async_terminate(self):
        """complete shutdown when HA exits. See self.async_shutdown for differences"""
        self.hass.services.async_remove(mlc.DOMAIN, mlc.SERVICE_REQUEST)
        for device in MerossApi.active_devices():
            await device.async_shutdown()
        for profile in MerossApi.active_profiles():
            await profile.async_shutdown()
        await super().async_shutdown()
        await MerossHttpClient.async_shutdown_session()
        self._mqtt_connection = None

    def build_device(self, device_id: str, config_entry: ConfigEntry) -> MerossDevice:
        """
        scans device descriptor to build a 'slightly' specialized MerossDevice
        The base MerossDevice class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        if device_id != config_entry.data.get(mlc.CONF_DEVICE_ID):
            # shouldnt really happen: it means we have a 'critical' bug in our config entry/flow management
            # or that the config_entry was tampered
            raise ConfigEntryError(
                "Unrecoverable device id mismatch. 'ConfigEntry.unique_id' "
                "does not match the configured 'device_id'. "
                "Please delete the entry and reconfigure it"
            )
        descriptor = MerossDeviceDescriptor(config_entry.data.get(mlc.CONF_PAYLOAD))
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

        ability = descriptor.ability
        digest = descriptor.digest

        if mc.KEY_HUB in digest:
            from .meross_device_hub import MerossDeviceHub

            class_base = MerossDeviceHub
        else:
            class_base = MerossDevice

        mixin_classes = []
        # put Toggle(X) mixin at the top of the class hierarchy
        # since the toggle feature could be related to a more
        # specialized entity than switch (see light for example)
        # this way the __init__ for toggle entity will be called
        # later and could check if a more specialized entity is
        # already in place for the very same channel
        if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
            from .switch import ToggleXMixin

            mixin_classes.append(ToggleXMixin)
        elif mc.NS_APPLIANCE_CONTROL_TOGGLE in ability:
            # toggle is older and superseded by togglex
            # so no need to handle it in case
            from .switch import ToggleMixin

            mixin_classes.append(ToggleMixin)

        for key_digest in digest:
            if key_digest in DIGEST_INITIALIZERS:
                init_descriptor = DIGEST_INITIALIZERS[key_digest]
                with self.exception_warning(
                    "initializing digest(%s) mixin", key_digest
                ):
                    module = import_module(
                        init_descriptor[0], "custom_components.meross_lan"
                    )
                    mixin_classes.append(getattr(module, init_descriptor[1]))

        # We must be careful when ordering the mixin and leave MerossDevice as last class.
        # Messing up with that will cause MRO to not resolve inheritance correctly.
        # see https://github.com/albertogeniola/MerossIot/blob/0.4.X.X/meross_iot/device_factory.py
        mixin_classes.append(class_base)
        # build a label to cache the set
        class_name = ""
        for m in mixin_classes:
            class_name = class_name + m.__name__
        if class_name in self._deviceclasses:
            class_type = self._deviceclasses[class_name]
        else:
            class_type = type(class_name, tuple(mixin_classes), {})
            self._deviceclasses[class_name] = class_type

        device = class_type(descriptor, config_entry)
        return device

    @property
    def mqtt_connection(self):
        if not (mqtt_connection := self._mqtt_connection):
            self._mqtt_connection = mqtt_connection = HAMQTTConnection(self)
        return mqtt_connection


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    LOGGER.debug("async_setup_entry (entry_id:%s)", config_entry.entry_id)

    api = MerossApi.api or MerossApi.get(hass)

    match ConfigEntryType.get_type_and_id(config_entry.unique_id):
        case (ConfigEntryType.DEVICE, device_id):
            if device_id in api.devices:
                assert api.devices[device_id] is None, "device already initialized"
            else:
                # this could happen when we add profile entries
                # after boot
                api.devices[device_id] = None
            device = api.build_device(device_id, config_entry)
            try:
                await device.async_setup_entry(hass, config_entry)
                api.devices[device_id] = device
                # this code needs to run after registering api.devices[device_id]
                # because of race conditions with profile entry loading
                for profile in api.active_profiles():
                    if profile.try_link(device):
                        break
                else:
                    api.try_link(device)
                device.start()
                return True
            except Exception as error:
                await device.async_shutdown()
                raise ConfigEntryError from error

        case (ConfigEntryType.PROFILE, profile_id):
            if profile_id in api.profiles:
                assert api.profiles[profile_id] is None
            else:
                # this could happen when we add profile entries
                # after boot
                api.profiles[profile_id] = None
            profile = MerossCloudProfile(profile_id, config_entry)
            try:
                await profile.async_init()
                await profile.async_setup_entry(hass, config_entry)
                api.profiles[profile_id] = profile
                # 'link' the devices already initialized
                for device in api.active_devices():
                    profile.try_link(device)
                return True
            except Exception as error:
                await profile.async_shutdown()
                raise ConfigEntryError from error

        case (ConfigEntryType.HUB, _):
            if not await api.mqtt_connection.async_mqtt_subscribe():
                raise ConfigEntryNotReady("MQTT unavailable")
            api.config_entry_id = config_entry.entry_id
            await api.entry_update_listener(hass, config_entry)
            await api.async_setup_entry(hass, config_entry)
            return True

        case _:
            raise ConfigEntryError(
                f"Unknown configuration type (entry_id:{config_entry.entry_id} title:'{config_entry.title}')"
            )


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    LOGGER.debug("async_unload_entry (entry_id:%s)", config_entry.entry_id)

    manager = MerossApi.managers[config_entry.entry_id]
    return await manager.async_unload_entry(hass, config_entry)


async def async_remove_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    LOGGER.debug("async_remove_entry (entry_id:%s)", config_entry.entry_id)

    match ConfigEntryType.get_type_and_id(config_entry.unique_id):
        case (ConfigEntryType.DEVICE, device_id):
            MerossApi.devices.pop(device_id)

        case (ConfigEntryType.PROFILE, profile_id):
            MerossApi.profiles.pop(profile_id)
            await MerossCloudProfileStore(profile_id).async_remove()
            credentials: MerossCloudCredentials = config_entry.data  # type: ignore
            await cloudapi.CloudApiClient(
                credentials=credentials, session=async_get_clientsession(hass)
            ).async_logout_safe()
