"""The Meross IoT local LAN integration."""
from __future__ import annotations

import asyncio
from json import dumps as json_dumps, loads as json_loads
import typing

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import storage
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
    CONF_PROTOCOL,
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    DOMAIN,
    SERVICE_REQUEST,
)
from .helpers import LOGGER, schedule_async_callback
from .meross_device import MerossDevice
from .meross_profile import ApiProfile, MerossCloudProfile, MQTTConnection
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
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice, ResponseCallbackType
    from .merossclient.cloudapi import MerossCloudCredentials
else:
    # In order to avoid a static dependency we resolve these
    # at runtime only when mqtt is actually needed in code
    mqtt_async_publish = None


class Store(storage.Store[dict]):

    VERSION = 1

    def __init__(self, hass: HomeAssistant):
        super().__init__(hass, Store.VERSION, DOMAIN)


class MerossApi(MQTTConnection, ApiProfile):
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """

    store: Store
    _unsub_mqtt_subscribe: Callable | None
    _unsub_mqtt_disconnected: Callable | None
    _unsub_mqtt_connected: Callable | None
    unsub_entry_update_listener: Callable | None

    @staticmethod
    def peek(hass: HomeAssistant) -> "MerossApi" | None:
        """helper to get the singleton"""
        return hass.data.get(DOMAIN)

    @staticmethod
    def peek_device(hass: HomeAssistant, device_id: str | None) -> MerossDevice | None:
        if (api := hass.data.get(DOMAIN)) is not None:
            return api.devices.get(device_id)
        return None

    @staticmethod
    def get(hass: HomeAssistant) -> "MerossApi":
        """helper to get the singleton eventually creating it"""
        api = hass.data.get(DOMAIN)
        if api is None:
            api = MerossApi(hass)
            hass.data[DOMAIN] = api
        return api

    @staticmethod
    async def async_update_profile(
        hass: HomeAssistant, credentials: "MerossCloudCredentials"
    ):
        api = MerossApi.get(hass)
        await api.async_load_store()
        profile_id = credentials.userid
        if profile_id not in api.profiles:
            profile = MerossCloudProfile(credentials)
            await profile.async_start()
        else:
            profile = api.profiles[profile_id]
            await profile.async_update_credentials(credentials)
        api.schedule_save_store()
        return profile

    def __init__(self, hass: HomeAssistant):
        ApiProfile.hass = hass
        ApiProfile.api = self
        super().__init__(self, CONF_PROFILE_ID_LOCAL)
        self.deviceclasses: dict[str, type] = {}
        self.key = ""
        self.store = None  # type: ignore
        self.store_loaded = hass.loop.create_future()
        self._unsub_mqtt_subscribe = None
        self._unsub_mqtt_disconnected = None
        self._unsub_mqtt_connected = None
        self.unsub_entry_update_listener = None
        self._mqtt_subscribing = False  # guard for asynchronous mqtt sub registration

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
            key = service_call.data.get(CONF_KEY, self.key)
            host = service_call.data.get(CONF_HOST)

            def response_callback(acknowledge: bool, header: dict, payload: dict):
                if service_call.data.get(CONF_NOTIFYRESPONSE):
                    self.hass.components.persistent_notification.async_create(
                        title="Meross LAN service response", message=json_dumps(payload)
                    )

            if device_id is not None:
                device = self.devices.get(device_id)
                if device is not None:
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
                    LOGGER.warning(
                        "MerossApi: cannot execute service call on %s - missing MQTT connectivity or device not registered",
                        device_id,
                    )
                    return
            elif host is None:
                LOGGER.warning(
                    "MerossApi: cannot execute service call (missing device_id and host)"
                )
                return
            # host is not None
            for device in self.devices.values():
                if device.host == host:
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

    def shutdown(self):
        super().shutdown()
        if self._unsub_mqtt_connected is not None:
            self._unsub_mqtt_connected()
            self._unsub_mqtt_connected = None
        if self._unsub_mqtt_disconnected is not None:
            self._unsub_mqtt_disconnected()
            self._unsub_mqtt_disconnected = None
        if self._unsub_mqtt_subscribe is not None:
            self._unsub_mqtt_subscribe()
            self._unsub_mqtt_subscribe = None
        if self.unsub_entry_update_listener is not None:
            self.unsub_entry_update_listener()
            self.unsub_entry_update_listener = None
        self.hass.data.pop(DOMAIN)

    def get_device_with_mac(self, macaddress: str):
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(":", "").lower()
        for device in self.devices.values():
            if device.descriptor.macAddress.replace(":", "").lower() == macaddress:
                return device
        return None

    async def async_load_store(self):
        if self.store_loaded is None:
            return
        if self.store is not None:
            await self.store_loaded
            return

        self.store = Store(self.hass)
        try:
            if data := await self.store.async_load():
                for profile_data in data.get("profiles", []):
                    profile = MerossCloudProfile(profile_data)
                    profile.schedule_start()

            if MEROSSDEBUG:
                for dummy_profile in MEROSSDEBUG.cloud_profiles:
                    if dummy_profile[mc.KEY_USERID_] in self.profiles:
                        continue
                    MerossCloudProfile(dummy_profile)
                    self.schedule_save_store()

        finally:
            self.store_loaded.set_result(True)
            self.store_loaded = None

    def schedule_save_store(self):
        def _data_func():
            profiles_data = [profile for profile in self.profiles.values()]
            return {"profiles": profiles_data}

        self.store.async_delay_save(_data_func, 60)

    def get_profiles_map(self):
        profiles_map: dict[str, str] = {CONF_PROFILE_ID_LOCAL: "local only"}
        for profile in self.profiles.values():
            profiles_map[profile.id] = profile.email
        return profiles_map

    def get_profile_by_id(self, profile_id: str):
        return self.profiles.get(profile_id)

    def get_profile_by_email(self, email: str):
        for profile in self.profiles.values():
            if profile.email == email:
                return profile

    def get_profile_by_key(self, key: str):
        for profile in self.profiles.values():
            if profile.key == key:
                return profile

    def build_device(self, device_id: str, entry: ConfigEntry) -> MerossDevice:
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
        if mc.NS_APPLIANCE_SYSTEM_RUNTIME in ability:
            from .sensor import RuntimeMixin

            mixin_classes.append(RuntimeMixin)
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
        self.devices[device_id] = device
        return device

    def mqtt_is_subscribed(self):
        return self._unsub_mqtt_subscribe is not None

    async def async_mqtt_register(self):
        """
        subscribe to the general meross mqtt 'publish' topic
        """
        if (self._unsub_mqtt_subscribe is None) and (not self._mqtt_subscribing):
            self._mqtt_subscribing = True
            try:

                from homeassistant.components import mqtt

                global mqtt_async_publish
                mqtt_async_publish = mqtt.async_publish
                self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                    self.hass, mc.TOPIC_DISCOVERY, self.async_mqtt_message
                )
                self._unsub_mqtt_disconnected = async_dispatcher_connect(
                    self.hass, mqtt.MQTT_DISCONNECTED, self.set_mqtt_disconnected
                )
                self._unsub_mqtt_connected = async_dispatcher_connect(
                    self.hass, mqtt.MQTT_CONNECTED, self.set_mqtt_connected
                )
                if mqtt.is_connected(self.hass):
                    self.set_mqtt_connected()

                if MEROSSDEBUG:

                    async def _async_random_disconnect():
                        if self._mqtt_subscribing:
                            pass
                        elif self._unsub_mqtt_subscribe is None:
                            if MEROSSDEBUG.mqtt_random_connect():
                                LOGGER.debug(
                                    "MerossApi(%s) random connect",
                                    self.id,
                                )
                                self._mqtt_subscribing = True
                                self._unsub_mqtt_subscribe = await mqtt.async_subscribe(
                                    self.hass,
                                    mc.TOPIC_DISCOVERY,
                                    self.async_mqtt_message,
                                )
                                self._unsub_mqtt_disconnected = (
                                    async_dispatcher_connect(
                                        self.hass,
                                        mqtt.MQTT_DISCONNECTED,
                                        self.set_mqtt_disconnected,
                                    )
                                )
                                self._unsub_mqtt_connected = async_dispatcher_connect(
                                    self.hass,
                                    mqtt.MQTT_CONNECTED,
                                    self.set_mqtt_connected,
                                )
                                self._mqtt_subscribing = False
                                if mqtt.is_connected(self.hass):
                                    self.set_mqtt_connected()

                        else:
                            if MEROSSDEBUG.mqtt_random_disconnect():
                                LOGGER.debug(
                                    "MerossApi(%s) random disconnect",
                                    self.id,
                                )
                                if self._unsub_mqtt_disconnected:
                                    self._unsub_mqtt_disconnected()
                                    self._unsub_mqtt_disconnected = None
                                if self._unsub_mqtt_connected:
                                    self._unsub_mqtt_connected()
                                    self._unsub_mqtt_connected = None
                                self._unsub_mqtt_subscribe()
                                self._unsub_mqtt_subscribe = None
                                if self._mqtt_is_connected:
                                    self.set_mqtt_disconnected()

                        schedule_async_callback(self.hass, 60, _async_random_disconnect)

                    schedule_async_callback(self.hass, 60, _async_random_disconnect)

            except:
                pass
            self._mqtt_subscribing = False

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
        LOGGER.debug(
            "MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)",
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
        try:
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
        except Exception as e:
            LOGGER.warning(
                "MerossApi: error in async_http_request(%s)", str(e) or type(e).__name__
            )

    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        self.key = config_entry.data.get(CONF_KEY) or ""


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Meross IoT local LAN component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    LOGGER.debug(
        "async_setup_entry { unique_id: %s, entry_id: %s }",
        entry.unique_id,
        entry.entry_id,
    )
    api = MerossApi.get(hass)

    await api.async_load_store()

    device_id = entry.data.get(CONF_DEVICE_ID)
    if (device_id is None) or (entry.data.get(CONF_PROTOCOL) != CONF_PROTOCOL_HTTP):
        """
        this is the MQTT Hub entry or a device which could/should use MQTT
        so we'll (try) register mqtt subscription for our topics
        """
        await api.async_mqtt_register()
    """
    this is a hell of race conditions: the previous mqtt_register could be overlapping (awaited)
    because of a different ConfigEntry request (where CONF_PROTOCOL != HTTP)
    here we need to be sure to delay load this entry until mqtt is in place (at least for those
    directly requiring MQTT)
    """
    if (device_id is None) or (entry.data.get(CONF_PROTOCOL) == CONF_PROTOCOL_MQTT):
        if not api.mqtt_is_subscribed():
            raise ConfigEntryNotReady("MQTT unavailable")

    if device_id is None:
        # this is the MQTT Hub entry
        api.key = entry.data.get(CONF_KEY) or ""
        api.unsub_entry_update_listener = entry.add_update_listener(
            api.entry_update_listener
        )
    else:
        device = api.build_device(device_id, entry)
        await asyncio.gather(
            *(
                hass.config_entries.async_forward_entry_setup(entry, platform)
                for platform in device.platforms.keys()
            )
        )

        if cloud_key := entry.data.get(CONF_CLOUD_KEY):
            # suggest to migrate:
            # we'll create a 'partial' cloud_profile with the data we
            # have since userid and key are enough, at least to mock a
            # working profile. We'll miss user email and cloud token
            # which are needed for display purposes and to eventually
            # query the device list. They'll be updated once the user
            # logins again to the profile (eventually)
            profile_id = device.descriptor.userId
            if profile_id and (profile_id not in api.profiles):
                MerossCloudProfile(
                    {
                        mc.KEY_USERID_: profile_id,
                        mc.KEY_EMAIL: f"{profile_id}@unknown.profile",
                        mc.KEY_KEY: cloud_key,
                    },
                )
                api.schedule_save_store()
                # TODO: raise an HA repair

        device.start()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.debug("async_unload_entry entry_id = %s", entry.entry_id)
    if (api := MerossApi.peek(hass)) is not None:
        device_id = entry.data.get(CONF_DEVICE_ID)
        if device_id is not None:
            LOGGER.debug("async_unload_entry device_id = %s", device_id)
            device = api.devices[device_id]
            if not await hass.config_entries.async_unload_platforms(
                entry, device.platforms.keys()
            ):
                return False
            api.devices.pop(device_id)
            await device.async_shutdown()
        # don't cleanup: the MerossApi is still needed to detect MQTT discoveries
        # if (not api.devices) and (len(hass.config_entries.async_entries(DOMAIN)) == 1):
        #    api.shutdown()
    return True
