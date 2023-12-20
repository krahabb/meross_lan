"""The Meross IoT local LAN integration."""
from __future__ import annotations

import asyncio
from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const as mlc
from .helpers import LOGGER, ApiProfile, ConfigEntriesHelper, schedule_async_callback
from .meross_device import MerossDevice
from .meross_profile import MerossCloudProfile, MerossCloudProfileStore, MQTTConnection
from .merossclient import (
    MEROSSDEBUG,
    MerossDeviceDescriptor,
    build_message,
    const as mc,
    get_default_payload,
)
from .merossclient.httpclient import MerossHttpClient

if typing.TYPE_CHECKING:
    from typing import Callable

    from homeassistant.core import ServiceCall, ServiceResponse
    from homeassistant.components.mqtt import async_publish as mqtt_async_publish
    from homeassistant.config_entries import ConfigEntry

    from .merossclient import KeyType, MerossMessageType, ResponseCallbackType


else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


class HAMQTTConnection(MQTTConnection):
    __slots__ = (
        "_unsub_mqtt_subscribe",
        "_unsub_mqtt_disconnected",
        "_unsub_mqtt_connected",
        "_mqtt_subscribing",
        "_unsub_random_disconnect",
    )

    def __init__(self, api: MerossApi):
        super().__init__(api, mlc.CONF_PROFILE_ID_LOCAL, ("homeassistant", 0))
        self._unsub_mqtt_subscribe: Callable | None = None
        self._unsub_mqtt_disconnected: Callable | None = None
        self._unsub_mqtt_connected: Callable | None = None
        self._mqtt_subscribing = False  # guard for asynchronous mqtt sub registration
        if MEROSSDEBUG:

            async def _async_random_disconnect():
                self._unsub_random_disconnect = schedule_async_callback(
                    ApiProfile.hass, 60, _async_random_disconnect
                )
                if self._mqtt_subscribing:
                    return
                elif self._unsub_mqtt_subscribe is None:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(DEBUG, "random connect")
                        await self.async_mqtt_subscribe()
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(DEBUG, "random disconnect")
                        await self.async_mqtt_unsubscribe()

            self._unsub_random_disconnect = schedule_async_callback(
                ApiProfile.hass, 60, _async_random_disconnect
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

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        response_callback: ResponseCallbackType | None = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        return ApiProfile.hass.async_create_task(
            self.async_mqtt_publish(
                device_id, namespace, method, payload, key, response_callback, messageid
            )
        )

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        response_callback: ResponseCallbackType | None = None,
        messageid: str | None = None,
    ):
        if method in mc.METHOD_ACK_MAP.keys():
            transaction = self._mqtt_transaction_init(
                namespace, method, response_callback
            )
            messageid = transaction.messageid
        else:
            transaction = None

        self.log(
            DEBUG,
            "MQTT PUBLISH device_id:(%s) method:(%s) namespace:(%s)",
            device_id,
            method,
            namespace,
        )
        await mqtt_async_publish(
            ApiProfile.hass,
            mc.TOPIC_REQUEST.format(device_id),
            json_dumps(
                build_message(
                    namespace,
                    method,
                    payload,
                    key,
                    mc.TOPIC_RESPONSE.format(device_id),
                    messageid,
                )
            ),
        )
        if transaction:
            return await self._async_mqtt_transaction_wait(transaction)  # type: ignore

    async def async_mqtt_publish_reply(
        self,
        device_id: str,
        message: MerossMessageType
    ):
        self.log(
            DEBUG,
            "MQTT PUBLISH REPLY device_id:(%s) method:(%s) namespace:(%s)",
            device_id,
            message[mc.KEY_HEADER][mc.KEY_METHOD],
            message[mc.KEY_HEADER][mc.KEY_NAMESPACE],
        )
        await mqtt_async_publish(
            ApiProfile.hass,
            mc.TOPIC_REQUEST.format(device_id),
            json_dumps(message)
        )


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
                hass = ApiProfile.hass
                self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                    hass, mc.TOPIC_DISCOVERY, self.async_mqtt_message
                )
                self._unsub_mqtt_disconnected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_DISCONNECTED, self._mqtt_disconnected
                )
                self._unsub_mqtt_connected = mqtt.async_dispatcher_connect(
                    hass, mqtt.MQTT_CONNECTED, self._mqtt_connected
                )
                # try to also get the HA broker conf
                with self.exception_warning(
                    "async_mqtt_subscribe: recovering broker conf"
                ):
                    mqtt_data = mqtt.get_mqtt_data(hass)
                    if mqtt_data and mqtt_data.client:
                        conf = mqtt_data.client.conf
                        self.broker = (conf[mqtt.CONF_BROKER], conf[mqtt.CONF_PORT])
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
        ApiProfile.api is just a cache to speed access
        """
        if mlc.DOMAIN not in hass.data:
            hass.data[mlc.DOMAIN] = api = MerossApi(hass)

            async def _async_unload_merossapi(_event) -> None:
                await api.async_shutdown()
                hass.data.pop(mlc.DOMAIN)

            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, _async_unload_merossapi
            )
            return api
        return hass.data[mlc.DOMAIN]

    def __init__(self, hass: HomeAssistant):
        ApiProfile.hass = hass
        ApiProfile.api = self
        super().__init__(mlc.CONF_PROFILE_ID_LOCAL)
        self._deviceclasses: dict[str, type] = {}
        self._mqtt_connection: HAMQTTConnection | None = None

        for config_entry in hass.config_entries.async_entries(mlc.DOMAIN):
            unique_id = config_entry.unique_id
            if (unique_id is None) or (unique_id == mlc.DOMAIN):
                continue
            unique_id = unique_id.split(".")
            if unique_id[0] == "profile":
                self.profiles[unique_id[1]] = None
            else:
                self.devices[unique_id[0]] = None

        async def async_service_request(service_call: ServiceCall) -> ServiceResponse:
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
            if mc.KEY_PAYLOAD in service_call.data:
                try:
                    payload = json_loads(service_call.data[mc.KEY_PAYLOAD])
                except Exception as e:
                    raise HomeAssistantError("Payload is not a valid JSON") from e
            elif method == mc.METHOD_GET:
                payload = get_default_payload(namespace)
            else:
                payload = {}  # likely failing the request...
            key = service_call.data.get(mlc.CONF_KEY, self.key)

            def response_callback(acknowledge: bool, header, payload):
                service_response["response"] = {
                    mc.KEY_HEADER: header,
                    mc.KEY_PAYLOAD: payload,
                }

            async def _async_device_request(device: MerossDevice):
                if protocol is mlc.CONF_PROTOCOL_MQTT:
                    return await device.async_mqtt_request(
                        namespace, method, payload, response_callback
                    )
                elif protocol is mlc.CONF_PROTOCOL_HTTP:
                    return await device.async_http_request(
                        namespace, method, payload, response_callback
                    )
                else:
                    return await device.async_request(
                        namespace, method, payload, response_callback
                    )

            if device_id:
                if device := self.devices.get(device_id):
                    await _async_device_request(device)
                    return service_response
                if (
                    protocol is not mlc.CONF_PROTOCOL_HTTP
                    and (mqtt_connection := self._mqtt_connection)
                    and mqtt_connection.mqtt_is_connected
                ):
                    await mqtt_connection.async_mqtt_publish(
                        device_id,
                        namespace,
                        method,
                        payload,
                        key,
                        response_callback,
                    )
                    return service_response

            if host:
                for device in self.active_devices():
                    if device.host == host:
                        await _async_device_request(device)
                        return service_response

                if protocol is not mlc.CONF_PROTOCOL_MQTT:
                    service_response["response"] = await self.async_http_request(
                        host, namespace, method, payload, key
                    )
                    return service_response

            raise HomeAssistantError(
                f"Unable to find a route to {device_id or host} using {protocol} protocol"
            )

        hass.services.async_register(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            async_service_request,
            supports_response=SupportsResponse.OPTIONAL,
        )
        return

    # interface: EntityManager
    async def async_shutdown(self):
        if self._mqtt_connection:
            await self._mqtt_connection.async_shutdown()
            self._mqtt_connection = None
        for device in self.active_devices():
            await device.async_shutdown()
        for profile in self.active_profiles():
            await profile.async_shutdown()
        await super().async_shutdown()
        ApiProfile.hass = None  # type: ignore
        ApiProfile.api = None  # type: ignore

    # interface: ApiProfile
    @property
    def allow_mqtt_publish(self):
        return True  # MerossApi still doesnt support configuring entry for this

    def attach_mqtt(self, device: MerossDevice):
        self.mqtt_connection.attach(device)

    # interface: self
    def build_device(self, device_id: str, config_entry: ConfigEntry) -> MerossDevice:
        """
        scans device descriptor to build a 'slightly' specialized MerossDevice
        The base MerossDevice class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        if device_id != config_entry.data.get(mlc.CONF_DEVICE_ID):
            # shouldnt really happen: it means we have a 'critical' bug in our config entry/flow management
            # or that the config_entry was tampered
            raise ConfigEntryError("Unrecoverable device id mismatch. 'ConfigEntry.unique_id' "
                                   "does not match the configured 'device_id'. "
                                   "Please delete the entry and reconfigure it")
        descriptor = MerossDeviceDescriptor(config_entry.data.get(mlc.CONF_PAYLOAD))
        if device_id != descriptor.uuid:
            # this could happen (#341 raised the suspect) if a working device
            # 'suddenly' starts talking with another one and doesn't recognize
            # the mismatch (the issue appears as the device usually keeps updating
            # the config_entry data from live communication). This behavior is being
            # fixed in 4.5.0 so that devices don't update wrong configurations 'in the wild'
            raise ConfigEntryError("Configuration data mismatch. Please refresh "
                                   "the configuration by hitting 'Configure' "
                                   "in the integration configuration page")

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
        # check MP3 before light since (HP110A) LightMixin
        # need to be overriden a bit for effect list
        if mc.NS_APPLIANCE_CONTROL_MP3 in ability:
            from .media_player import Mp3Mixin

            mixin_classes.append(Mp3Mixin)
        if mc.KEY_LIGHT in digest:
            from .light import LightMixin

            mixin_classes.append(LightMixin)
        if mc.NS_APPLIANCE_CONTROL_ELECTRICITY in ability:
            from .devices.mss import ElectricityMixin

            mixin_classes.append(ElectricityMixin)
        if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in ability:
            from .devices.mss import ConsumptionXMixin

            mixin_classes.append(ConsumptionXMixin)
        if mc.NS_APPLIANCE_CONFIG_OVERTEMP in ability:
            from .devices.mss import OverTempMixin

            mixin_classes.append(OverTempMixin)
        if mc.KEY_SPRAY in digest:
            from .select import SprayMixin

            mixin_classes.append(SprayMixin)
        if mc.KEY_GARAGEDOOR in digest:
            from .cover import GarageMixin

            mixin_classes.append(GarageMixin)
        if mc.NS_APPLIANCE_ROLLERSHUTTER_STATE in ability:
            from .cover import RollerShutterMixin

            mixin_classes.append(RollerShutterMixin)
        if mc.KEY_THERMOSTAT in digest:
            from .devices.mts200 import ThermostatMixin

            mixin_classes.append(ThermostatMixin)
        if mc.KEY_DIFFUSER in digest:
            from .devices.mod100 import DiffuserMixin

            mixin_classes.append(DiffuserMixin)
        if mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS in ability:
            from .devices.mts200 import ScreenBrightnessMixin

            mixin_classes.append(ScreenBrightnessMixin)
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

    async def async_http_request(
        self,
        host: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
    ):
        with self.exception_warning("async_http_request"):
            _httpclient: MerossHttpClient = getattr(self, "_httpclient", None)  # type: ignore
            if _httpclient:
                _httpclient.host = host
                _httpclient.key = key
            else:
                self._httpclient = _httpclient = MerossHttpClient(
                    host, key, async_get_clientsession(self.hass), LOGGER
                )
            return await _httpclient.async_request(namespace, method, payload)
        return None


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    unique_id = config_entry.unique_id
    assert unique_id, "unique_id must be set"
    LOGGER.debug(
        "async_setup_entry { unique_id: %s, entry_id: %s }",
        unique_id,
        config_entry.entry_id,
    )
    api = MerossApi.api or MerossApi.get(hass)

    if unique_id == mlc.DOMAIN:
        # MQTT Hub entry
        await api.entry_update_listener(hass, config_entry)
        if not await api.mqtt_connection.async_mqtt_subscribe():
            raise ConfigEntryNotReady("MQTT unavailable")
        await api.async_setup_entry(hass, config_entry)
        return True

    unique_id = unique_id.split(".")
    if unique_id[0] == "profile":
        # profile entry
        profile_id = unique_id[1]
        if profile_id in api.profiles:
            assert api.profiles[profile_id] is None
        else:
            # this could happen when we add profile entries
            # after boot
            api.profiles[profile_id] = None
        profile = MerossCloudProfile(config_entry)
        try:
            await profile.async_start()
            await profile.async_setup_entry(hass, config_entry)
            api.profiles[profile_id] = profile
            # 'link' the devices already initialized
            for device in api.active_devices():
                if device.descriptor.userId == profile_id:
                    profile.link(device)
            return True
        except Exception as error:
            await profile.async_shutdown()
            raise ConfigEntryError from error

    # device entry
    device_id = unique_id[0]
    if device_id in api.devices:
        assert api.devices[device_id] is None, "device already initialized"
    else:
        # this could happen when we add profile entries
        # after boot
        api.devices[device_id] = None
    device = api.build_device(device_id, config_entry)
    try:
        await device.async_setup_entry(hass, config_entry)
        device.start()
        api.devices[device_id] = device
        # this code needs to run after registering api.devices[device_id]
        # because of race conditions with profile entry loading
        profile_id = device.descriptor.userId
        if profile_id in api.profiles:
            # the profile is somehow configured, either disabled or not
            if profile := api.profiles[profile_id]:
                profile.link(device)
        else:
            # trigger a cloud profile discovery if we guess it reasonable
            if profile_id and (config_entry.data.get(mlc.CONF_CLOUD_KEY) == device.key):
                helper = ConfigEntriesHelper(hass)
                flow_unique_id = f"profile.{profile_id}"
                if not helper.get_config_flow(flow_unique_id):
                    await hass.config_entries.flow.async_init(
                        mlc.DOMAIN,
                        context={
                            "source": SOURCE_INTEGRATION_DISCOVERY,
                            "unique_id": flow_unique_id,
                            "title_placeholders": {"name": "unknown cloud profile"},
                        },
                        data={
                            mc.KEY_USERID_: profile_id,
                        },
                    )
        return True
    except Exception as error:
        await device.async_shutdown()
        raise ConfigEntryError from error


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    LOGGER.debug(
        "async_unload_entry { unique_id: %s, entry_id: %s }",
        config_entry.unique_id,
        config_entry.entry_id,
    )

    manager = ApiProfile.managers[config_entry.entry_id]
    if not await manager.async_unload_entry(hass, config_entry):
        return False

    if manager is not ApiProfile.api:
        await manager.async_shutdown()

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    unique_id = entry.unique_id
    LOGGER.debug(
        "async_remove_entry { unique_id: %s, entry_id: %s }",
        unique_id,
        entry.entry_id,
    )
    if unique_id == mlc.DOMAIN:
        return

    assert unique_id
    unique_id = unique_id.split(".")

    if unique_id[0] == "profile":
        profile_id = unique_id[1]
        ApiProfile.profiles.pop(profile_id)
        await MerossCloudProfileStore(profile_id).async_remove()
        return

    ApiProfile.devices.pop(unique_id[0])
