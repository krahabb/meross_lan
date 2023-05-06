"""The Meross IoT local LAN integration."""
from __future__ import annotations

import asyncio
from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_CLOUD_KEY,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_NOTIFYRESPONSE,
    CONF_PAYLOAD,
    CONF_PROFILE_ID_LOCAL,
    CONF_PROTOCOL_HTTP,
    DOMAIN,
    SERVICE_REQUEST,
)
from .helpers import LOGGER, ApiProfile, ConfigEntriesHelper, schedule_async_callback
from .meross_device import MerossDevice
from .meross_profile import MerossCloudProfile, MerossCloudProfileStore, MQTTConnection
from .merossclient import (
    MEROSSDEBUG,
    KeyType,
    MerossDeviceDescriptor,
    build_payload,
    const as mc,
    get_default_payload,
)
from .merossclient.httpclient import MerossHttpClient

if typing.TYPE_CHECKING:
    from typing import Callable

    from homeassistant.components.mqtt import async_publish as mqtt_async_publish
    from homeassistant.config_entries import ConfigEntry

    from .meross_device import ResponseCallbackType

else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


class MerossApi(MQTTConnection, ApiProfile):
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """

    @staticmethod
    def get(hass: HomeAssistant) -> MerossApi:
        if DOMAIN not in hass.data:
            hass.data[DOMAIN] = MerossApi(hass)
        return hass.data[DOMAIN]

    def __init__(self, hass: HomeAssistant):
        ApiProfile.hass = hass
        ApiProfile.api = self
        super().__init__(self, CONF_PROFILE_ID_LOCAL)
        self.deviceclasses: dict[str, type] = {}
        self._key = ""
        self._unsub_mqtt_subscribe: Callable | None = None
        self._unsub_mqtt_disconnected: Callable | None = None
        self._unsub_mqtt_connected: Callable | None = None
        self._mqtt_subscribing = False  # guard for asynchronous mqtt sub registration
        self._unsub_random_disconnect = None

        for config_entry in hass.config_entries.async_entries(DOMAIN):
            unique_id = config_entry.unique_id
            if (unique_id is None) or (unique_id == DOMAIN):
                continue
            unique_id = unique_id.split(".")
            if unique_id[0] == "profile":
                self.profiles[unique_id[1]] = None
            else:
                self.devices[unique_id[0]] = None

        async def async_service_request(service_call):
            device_id = service_call.data.get(CONF_DEVICE_ID)
            namespace = service_call.data[mc.KEY_NAMESPACE]
            method = service_call.data.get(mc.KEY_METHOD, mc.METHOD_GET)
            if mc.KEY_PAYLOAD in service_call.data:
                payload = json_loads(service_call.data[mc.KEY_PAYLOAD])
            elif method == mc.METHOD_GET:
                payload = get_default_payload(namespace)
            else:
                payload = {}  # likely failing the request...
            key = service_call.data.get(CONF_KEY, self._key)
            host = service_call.data.get(CONF_HOST)

            def response_callback(acknowledge: bool, header: dict, payload: dict):
                if service_call.data.get(CONF_NOTIFYRESPONSE):
                    self.hass.components.persistent_notification.async_create(
                        title="Meross LAN service response", message=json_dumps(payload)
                    )

            if device_id is not None:
                if (device := self.devices.get(device_id)) is not None:
                    await device.async_request(
                        namespace, method, payload, response_callback
                    )
                    return
                # device not registered (yet?) try direct MQTT
                if self.mqtt_is_connected:
                    await self.async_mqtt_publish(
                        device_id, namespace, method, payload, key
                    )
                    return
                if host is None:
                    self.warning(
                        "cannot execute service call on %s - missing MQTT connectivity or device not registered",
                        device_id,
                    )
                    return
            elif host is None:
                self.warning("cannot execute service call (missing device_id and host)")
                return
            # host is not None
            for device in self.devices.values():
                if (device is not None) and (device.host == host):
                    await device.async_request(
                        namespace, method, payload, response_callback
                    )
                    return
            self.hass.async_create_task(
                self.async_http_request(
                    host, namespace, method, payload, key, response_callback
                )
            )

        hass.services.async_register(DOMAIN, SERVICE_REQUEST, async_service_request)
        return

    async def async_shutdown(self):
        if self._unsub_random_disconnect is not None:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        if self._unsub_mqtt_connected is not None:
            self._unsub_mqtt_connected()
            self._unsub_mqtt_connected = None
        if self._unsub_mqtt_disconnected is not None:
            self._unsub_mqtt_disconnected()
            self._unsub_mqtt_disconnected = None
        if self._unsub_mqtt_subscribe is not None:
            self._unsub_mqtt_subscribe()
            self._unsub_mqtt_subscribe = None
        for device in self.active_devices():
            await device.async_shutdown()
        for profile in self.active_profiles():
            await profile.async_shutdown()
        await super().async_shutdown()
        ApiProfile.hass = None  # type: ignore
        ApiProfile.api = None  # type: ignore

    @property
    def key(self) -> str | None:
        return self._key

    @property
    def logtag(self):
        return "MerossApi"

    @property
    def broker(self):
        # TODO: recover the HA MQTT conf BROKER:PORT
        return "homeassistant", 0

    def build_device(self, entry: ConfigEntry) -> MerossDevice:
        """
        scans device descriptor to build a 'slightly' specialized MerossDevice
        The base MerossDevice class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD))
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
            from .sensor import ElectricityMixin

            mixin_classes.append(ElectricityMixin)
        if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in ability:
            from .sensor import ConsumptionMixin

            mixin_classes.append(ConsumptionMixin)
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
            from .number import ScreenBrightnessMixin

            mixin_classes.append(ScreenBrightnessMixin)
        # We must be careful when ordering the mixin and leave MerossDevice as last class.
        # Messing up with that will cause MRO to not resolve inheritance correctly.
        # see https://github.com/albertogeniola/MerossIot/blob/0.4.X.X/meross_iot/device_factory.py
        mixin_classes.append(class_base)
        # build a label to cache the set
        class_name = ""
        for m in mixin_classes:
            class_name = class_name + m.__name__
        if class_name in self.deviceclasses:
            class_type = self.deviceclasses[class_name]
        else:
            class_type = type(class_name, tuple(mixin_classes), {})
            self.deviceclasses[class_name] = class_type

        device = class_type(descriptor, entry)
        return device

    def mqtt_is_subscribed(self):
        return self._unsub_mqtt_subscribe is not None

    async def async_mqtt_register(self) -> bool:
        """
        subscribe to the general meross mqtt 'publish' topic
        """
        if self._unsub_mqtt_subscribe is not None:
            return True
        """
        TODO: use a future to gate the calls instead of the dumb
        _mqtt_subscribing lock
        """
        if self._mqtt_subscribing is True:
            return False

        with self.exception_warning("async_mqtt_register"):
            from homeassistant.components import mqtt

            global mqtt_async_publish
            mqtt_async_publish = mqtt.async_publish
            self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                self.hass, mc.TOPIC_DISCOVERY, self.async_mqtt_message
            )
            self._unsub_mqtt_disconnected = async_dispatcher_connect(
                self.hass, mqtt.MQTT_DISCONNECTED, self._mqtt_disconnected
            )
            self._unsub_mqtt_connected = async_dispatcher_connect(
                self.hass, mqtt.MQTT_CONNECTED, self._mqtt_connected
            )
            if mqtt.is_connected(self.hass):
                self._mqtt_connected()

            if MEROSSDEBUG and (self._unsub_random_disconnect is None):

                async def _async_random_disconnect():
                    self._unsub_random_disconnect = schedule_async_callback(
                        self.hass, 60, _async_random_disconnect
                    )
                    if self._mqtt_subscribing:
                        pass
                    elif self._unsub_mqtt_subscribe is None:
                        if MEROSSDEBUG.mqtt_random_connect():
                            self.log(DEBUG, "random connect")
                            self._mqtt_subscribing = True
                            self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                                self.hass,
                                mc.TOPIC_DISCOVERY,
                                self.async_mqtt_message,
                            )
                            self._unsub_mqtt_disconnected = async_dispatcher_connect(
                                self.hass,
                                mqtt.MQTT_DISCONNECTED,
                                self._mqtt_disconnected,
                            )
                            self._unsub_mqtt_connected = async_dispatcher_connect(
                                self.hass,
                                mqtt.MQTT_CONNECTED,
                                self._mqtt_connected,
                            )
                            self._mqtt_subscribing = False
                            if mqtt.is_connected(self.hass):
                                self._mqtt_connected()

                    else:
                        if MEROSSDEBUG.mqtt_random_disconnect():
                            self.log(DEBUG, "random disconnect")
                            if self._unsub_mqtt_disconnected:
                                self._unsub_mqtt_disconnected()
                                self._unsub_mqtt_disconnected = None
                            if self._unsub_mqtt_connected:
                                self._unsub_mqtt_connected()
                                self._unsub_mqtt_connected = None
                            self._unsub_mqtt_subscribe()
                            self._unsub_mqtt_subscribe = None
                            if self._mqtt_is_connected:
                                self._mqtt_disconnected()

                self._unsub_random_disconnect = schedule_async_callback(
                    self.hass, 60, _async_random_disconnect
                )

        self._mqtt_subscribing = False
        return self._unsub_mqtt_subscribe is not None

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        return self.hass.async_create_task(
            self.async_mqtt_publish(
                device_id, namespace, method, payload, key, messageid
            )
        )

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        self.log(
            DEBUG,
            "MQTT SEND device_id:(%s) method:(%s) namespace:(%s)",
            device_id,
            method,
            namespace,
        )
        await mqtt_async_publish(
            self.hass,
            mc.TOPIC_REQUEST.format(device_id),
            json_dumps(
                build_payload(
                    namespace,
                    method,
                    payload,
                    key,
                    mc.TOPIC_RESPONSE.format(device_id),
                    messageid,
                )
            ),
        )

    async def async_http_request(
        self,
        host: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        callback_or_device: ResponseCallbackType | MerossDevice | None = None,
    ):
        with self.exception_warning("async_http_request"):
            _httpclient: MerossHttpClient = getattr(self, "_httpclient", None)  # type: ignore
            if _httpclient is None:
                _httpclient = MerossHttpClient(
                    host, key, async_get_clientsession(self.hass), LOGGER
                )
                self._httpclient = _httpclient
            else:
                _httpclient.host = host
                _httpclient.key = key

            response = await _httpclient.async_request(namespace, method, payload)
            r_header = response[mc.KEY_HEADER]
            if callback_or_device is not None:
                if isinstance(callback_or_device, MerossDevice):
                    callback_or_device.receive(
                        r_header, response[mc.KEY_PAYLOAD], CONF_PROTOCOL_HTTP
                    )
                else:
                    callback_or_device(
                        r_header[mc.KEY_METHOD] != mc.METHOD_ERROR,
                        r_header,
                        response[mc.KEY_PAYLOAD],
                    )

    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        self._key = config_entry.data.get(CONF_KEY) or ""


async def async_setup(hass: HomeAssistant, config: dict):
    """
    Set up the Meross IoT local LAN component.
    "async_setup" is just called when loading entries for
    the first time after boot but the api might need
    initialization for the ConfigFlow.
    'Our' truth singleton is saved in hass.data[DOMAIN] and
    ApiProfile.api is just a cache to speed access
    """
    api = MerossApi.get(hass)

    async def _async_unload_merossapi(_event) -> None:
        await api.async_shutdown()
        hass.data.pop(DOMAIN)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_unload_merossapi)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    unique_id = entry.unique_id
    assert unique_id, "unique_id must be set"
    LOGGER.debug(
        "async_setup_entry { unique_id: %s, entry_id: %s }",
        unique_id,
        entry.entry_id,
    )
    api = ApiProfile.api

    if unique_id == DOMAIN:
        # MQTT Hub entry
        if not await api.async_mqtt_register():
            raise ConfigEntryNotReady("MQTT unavailable")
        api._key = entry.data.get(CONF_KEY) or ""
        api.listen_entry_update(entry)
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
        profile = MerossCloudProfile(entry.data)
        try:
            await profile.async_start()
            # 'link' the devices already initialized
            for device in api.active_devices():
                if device.descriptor.userId == profile_id:
                    profile.link(device)
            profile.listen_entry_update(entry)
            api.profiles[profile_id] = profile
            return True
        except Exception as error:
            await profile.async_shutdown()
            raise ConfigEntryError from error

    # device entry
    device_id = unique_id[0]
    assert api.devices.get(device_id) is None, "device already initialized"
    api.devices[device_id] = device = api.build_device(entry)
    try:
        await asyncio.gather(
            *(
                hass.config_entries.async_forward_entry_setup(entry, platform)
                for platform in device.platforms.keys()
            )
        )
        profile_id = device.descriptor.userId
        if profile_id in api.profiles:
            # the profile is somehow configured, either disabled or not
            if (profile := api.profiles[profile_id]) is not None:
                profile.link(device)
        else:
            # trigger a cloud profile discovery if we guess it reasonable
            if profile_id and (entry.data.get(CONF_CLOUD_KEY) == device.key):
                helper = ConfigEntriesHelper(hass)
                flow_unique_id = f"profile.{profile_id}"
                if helper.get_config_flow(flow_unique_id) is None:
                    await hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={
                            "source": SOURCE_INTEGRATION_DISCOVERY,
                            "unique_id": flow_unique_id,
                            "title_placeholders": {"name": "unknown cloud profile"},
                        },
                        data={
                            mc.KEY_USERID_: profile_id,
                        },
                    )

        device.start()
        return True
    except Exception as error:
        await device.async_shutdown()
        raise ConfigEntryError from error


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unique_id = entry.unique_id
    LOGGER.debug(
        "async_unload_entry { unique_id: %s, entry_id: %s }",
        unique_id,
        entry.entry_id,
    )

    api = ApiProfile.api

    if unique_id == DOMAIN:
        # MQTT Hub entry
        api.unlisten_entry_update()
        return True

    unique_id = unique_id.split(".")  # type: ignore
    if unique_id[0] == "profile":
        profile = ApiProfile.profiles[unique_id[1]]
        assert profile is not None
        await profile.async_shutdown()
        return True

    device = ApiProfile.devices[unique_id[0]]
    assert device is not None
    if not await hass.config_entries.async_unload_platforms(
        entry, device.platforms.keys()
    ):
        return False
    await device.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    unique_id = entry.unique_id
    assert unique_id
    LOGGER.debug(
        "async_remove_entry { unique_id: %s, entry_id: %s }",
        unique_id,
        entry.entry_id,
    )
    if unique_id == DOMAIN:
        return

    unique_id = unique_id.split(".")

    if unique_id[0] == "profile":
        profile_id = unique_id[1]
        ApiProfile.profiles.pop(profile_id)
        await MerossCloudProfileStore(profile_id).async_remove()
        return

    device_id = unique_id[0]
    ApiProfile.devices.pop(device_id)
