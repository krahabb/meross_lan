"""The Meross IoT local LAN integration."""
from __future__ import annotations
from typing import Callable, Dict, Optional, Union
from time import time
from datetime import datetime, timedelta
from logging import WARNING, INFO, DEBUG
from json import (
    dumps as json_dumps,
    loads as json_loads,
)
from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.mqtt.const import MQTT_DISCONNECTED
from homeassistant.helpers import device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryNotReady

from .merossclient import (
    const as mc, KeyType,
    MerossDeviceDescriptor, MerossHttpClient,
    build_payload, build_default_payload_get, get_replykey,
)
from .meross_device import MerossDevice
from .helpers import (
    LOGGER, LOGGER_trap,
    mqtt_publish, mqtt_is_connected,
)
from .const import (
    DOMAIN, SERVICE_REQUEST,
    CONF_HOST, CONF_PROTOCOL, CONF_PROTOCOL_HTTP, CONF_PROTOCOL_MQTT,
    CONF_DEVICE_ID, CONF_KEY, CONF_CLOUD_KEY, CONF_PAYLOAD,
    CONF_POLLING_PERIOD_DEFAULT,
    PARAM_UNAVAILABILITY_TIMEOUT,PARAM_HEARTBEAT_PERIOD,
)



class MerossApi:
    """
    central meross_lan management (singleton) class which handles devices
    and MQTT discovery and message routing
    """
    KEY_STARTTIME = '__starttime'
    KEY_REQUESTTIME = '__requesttime'


    @staticmethod
    def peek(hass: HomeAssistant) -> "MerossApi" | None:
        """helper to get the singleton"""
        return hass.data.get(DOMAIN)

    @staticmethod
    def peek_device(hass: HomeAssistant, device_id:str) -> MerossDevice | None:
        if (api := hass.data.get(DOMAIN)) is not None:
            return api.devices.get(device_id)
        return None

    @staticmethod
    def get(hass: HomeAssistant) -> "MerossApi" | None:
        """helper to get the singleton eventually creating it"""
        api = hass.data.get(DOMAIN)
        if api is None:
            api = MerossApi(hass)
            hass.data[DOMAIN] = api
        return api


    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.key = None
        self.cloud_key = None
        self.deviceclasses: Dict[str, object] = {}
        self.devices: Dict[str, MerossDevice] = {}
        self.discovering: Dict[str, dict] = {}
        self.mqtt_subscribing = False # guard for asynchronous mqtt sub registration
        self.unsub_mqtt_subscribe = None
        self.unsub_mqtt_disconnected = None
        self.unsub_entry_update_listener = None
        self.unsub_discovery_callback = None

        async def async_update_data():
            """
            data fetch and control moved to MerossDevice
            """
            return None

        self.coordinator = DataUpdateCoordinator(
            hass,
            LOGGER,
            # Name of the data. For logging purposes.
            name=DOMAIN,
            update_method=async_update_data,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=CONF_POLLING_PERIOD_DEFAULT),
        )

        @callback
        def _request(service_call):
            device_id = service_call.data.get(CONF_DEVICE_ID)
            namespace = service_call.data.get(mc.KEY_NAMESPACE)
            method = service_call.data.get(mc.KEY_METHOD)
            payload = json_loads(service_call.data.get(mc.KEY_PAYLOAD, "{}"))
            key = service_call.data.get(CONF_KEY, self.key)
            host = service_call.data.get(CONF_HOST)

            if device_id is not None:
                device = self.devices.get(device_id)
                if device is not None:
                    device.request(namespace, method, payload)
                    return
                # device not registered (yet?) try direct MQTT
                if (self.unsub_mqtt_subscribe is not None) and mqtt_is_connected(self.hass):
                    self.mqtt_publish(device_id, namespace, method, payload, key)
                    return
                if host is None:
                    LOGGER.warning("MerossApi: cannot execute service call on %s - missing MQTT connectivity or device not registered", device_id)
                    return
            elif host is None:
                LOGGER.warning("MerossApi: cannot execute service call (missing device_id and host)")
                return
            # host is not None
            for device in self.devices.values():
                if device.host == host:
                    device.request(namespace, method, payload)
                    return

            self.hass.async_create_task(
                self.async_http_request(host, namespace, method, payload, key, None)
                )
            return

        hass.services.async_register(DOMAIN, SERVICE_REQUEST, _request)
        return


    def shutdown(self):
        if self.unsub_mqtt_disconnected is not None:
            self.unsub_mqtt_disconnected()
            self.unsub_mqtt_disconnected = None
        if self.unsub_mqtt_subscribe is not None:
            self.unsub_mqtt_subscribe()
            self.unsub_mqtt_subscribe = None
        if self.unsub_entry_update_listener is not None:
            self.unsub_entry_update_listener()
            self.unsub_entry_update_listener = None
        if self.unsub_discovery_callback is not None:
            self.unsub_discovery_callback()
            self.unsub_discovery_callback = None
        self.hass.data.pop(DOMAIN)


    @property
    def mqtt_registered(self) -> bool:
        return self.unsub_mqtt_subscribe is not None


    @callback
    async def async_mqtt_receive(self, msg):
        """ global MQTT discovery (for unregistered) and routing (for registered devices)"""
        try:
            message = json_loads(msg.payload)
            header = message[mc.KEY_HEADER]
            device_id = msg.topic.split("/")[2]
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug("MerossApi: MQTT RECV device_id:(%s) method:(%s) namespace:(%s)", device_id, header[mc.KEY_METHOD], header[mc.KEY_NAMESPACE])
            if (device := self.devices.get(device_id)) is None:
                # lookout for any disabled/ignored entry
                mqtt_entry_present = False
                for domain_entry in self.hass.config_entries.async_entries(DOMAIN):
                    if (domain_entry.unique_id == device_id):
                        # entry already present...
                        #if domain_entry.disabled_by == DOMAIN:
                            # we previously disabled this one due to extended anuavailability
                            #await self.hass.config_entries.async_set_disabled_by(domain_entry.entry_id, None)
                        # skip discovery anyway
                        msg_reason = "disabled" if domain_entry.disabled_by is not None \
                            else "ignored" if domain_entry.source == "ignore" \
                                else "unknown"
                        LOGGER_trap(INFO, 14400, "Ignoring discovery for device_id: %s (ConfigEntry is %s)", device_id, msg_reason)
                        return
                    mqtt_entry_present |= domain_entry.unique_id == DOMAIN
                #also skip discovered integrations waiting in HA queue
                for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
                    flow_unique_id = flow.get("context", {}).get("unique_id")
                    if (flow_unique_id == device_id):
                        LOGGER_trap(INFO, 14400, "Ignoring discovery for device_id: %s (ConfigEntry is in progress)", device_id)
                        return
                    mqtt_entry_present |= flow_unique_id == DOMAIN

                if (not mqtt_entry_present):
                    await self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={ "source": "hub" },
                        data=None,
                    )

                replykey = get_replykey(header, self.key)
                if replykey is not self.key:
                    LOGGER_trap(WARNING, 300, "Meross discovery key error for device_id: %s", device_id)
                    if self.key is not None:# we're using a fixed key in discovery so ignore this device
                        return

                discovered = self.discovering.get(device_id)
                if discovered is None:
                    # new device discovered: try to determine the capabilities
                    self.mqtt_publish_get(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, replykey)
                    epoch = time()
                    self.discovering[device_id] = {
                        MerossApi.KEY_STARTTIME: epoch,
                        MerossApi.KEY_REQUESTTIME: epoch
                        }
                    if self.unsub_discovery_callback is None:
                        self.unsub_discovery_callback = async_track_point_in_utc_time(
                            self.hass,
                            self.discovery_callback,
                            datetime.fromtimestamp(epoch + PARAM_UNAVAILABILITY_TIMEOUT + 2)
                        )

                else:
                    if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                        namespace = header[mc.KEY_NAMESPACE]
                        if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
                            discovered[mc.NS_APPLIANCE_SYSTEM_ALL] = message[mc.KEY_PAYLOAD]
                            self.mqtt_publish_get(device_id, mc.NS_APPLIANCE_SYSTEM_ABILITY, replykey)
                            discovered[MerossApi.KEY_REQUESTTIME] = time()
                            return
                        elif namespace == mc.NS_APPLIANCE_SYSTEM_ABILITY:
                            if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                                self.mqtt_publish_get(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, replykey)
                                discovered[MerossApi.KEY_REQUESTTIME] = time()
                                return
                            payload = message[mc.KEY_PAYLOAD]
                            payload.update(discovered[mc.NS_APPLIANCE_SYSTEM_ALL])
                            self.discovering.pop(device_id)
                            if (len(self.discovering) == 0) and self.unsub_discovery_callback:
                                self.unsub_discovery_callback()
                                self.unsub_discovery_callback = None
                            await self.hass.config_entries.flow.async_init(
                                DOMAIN,
                                context={ "source": SOURCE_DISCOVERY },
                                data={
                                    CONF_DEVICE_ID: device_id,
                                    CONF_PAYLOAD: payload,
                                    CONF_KEY: replykey
                                },
                            )
                            return

            else:
                device.mqtt_receive(header, message[mc.KEY_PAYLOAD])

        except Exception as error:
            LOGGER.debug("MerossApi: async_mqtt_receive exception:(%s) payload:(%s)", str(error), str(msg))

        return


    async def async_mqtt_register(self):
        """
        subscribe to the general meross mqtt 'publish' topic
        """
        if (self.unsub_mqtt_subscribe is None) and (not self.mqtt_subscribing):
            self.mqtt_subscribing = True
            try:
                @callback
                def mqtt_disconnected():
                    for device in self.devices.values():
                        device.mqtt_disconnected()

                self.unsub_mqtt_subscribe = await self.hass.components.mqtt.async_subscribe(mc.TOPIC_DISCOVERY, self.async_mqtt_receive)
                self.unsub_mqtt_disconnected = async_dispatcher_connect(self.hass, MQTT_DISCONNECTED, mqtt_disconnected)
                #self.unsub_mqtt_connected = async_dispatcher_connect(self.hass, MQTT_CONNECTED, mqtt_connected)
            except:
                pass
            self.mqtt_subscribing = False


    def get_device_with_mac(self, macaddress:str) -> MerossDevice:
        # macaddress from dhcp discovery is already stripped/lower but...
        macaddress = macaddress.replace(':', '').lower()
        for device in self.devices.values():
            if device.descriptor.macAddress.replace(':', '').lower() == macaddress:
                return device
        return False


    def build_device(self, device_id: str, entry: ConfigEntry) -> MerossDevice:
        """
        scans device descriptor to build a 'slightly' specialized MerossDevice
        The base MerossDevice class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD, {}))
        ability = descriptor.ability
        digest = descriptor.digest

        if (mc.KEY_HUB in digest):
            from .meross_device_hub import MerossDeviceHub
            class_base = MerossDeviceHub
        else:
            class_base = MerossDevice

        mixin_classes = list()
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
        class_name = ''
        for m in mixin_classes:
            class_name = class_name + m.__name__
        if class_name in self.deviceclasses:
            class_type = self.deviceclasses[class_name]
        else:
            class_type = type(class_name, tuple(mixin_classes), {})
            self.deviceclasses[class_name] = class_type

        device = class_type(self, descriptor, entry)
        self.devices[device_id] = device
        self.update_polling_period()

        try:
            # try block since this is not critical and api has recently changed
            device_registry.async_get(self.hass).async_get_or_create(
                config_entry_id = entry.entry_id,
                connections = {(device_registry.CONNECTION_NETWORK_MAC, descriptor.macAddress)},
                identifiers = {(DOMAIN, device_id)},
                manufacturer = mc.MANUFACTURER,
                name = descriptor.productname,
                model = descriptor.productmodel,
                sw_version = descriptor.firmware.get(mc.KEY_VERSION)
            )
        except:
            pass

        return device


    def mqtt_publish(self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str = None
    ) -> None:
        LOGGER.debug("MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)
        mqtt_publish(
            self.hass,
            mc.TOPIC_REQUEST.format(device_id),
            json_dumps(build_payload(
                namespace, method, payload, key,
                mc.TOPIC_RESPONSE.format(device_id), messageid))
            )


    def mqtt_publish_get(self,
        device_id: str,
        namespace: str,
        key: KeyType = None
    ) -> None:
        self.mqtt_publish(
            device_id,
            namespace,
            mc.METHOD_GET,
            build_default_payload_get(namespace),
            key
        )


    async def async_http_request(self,
        host: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        callback_or_device: Union[Callable, MerossDevice] = None # pylint: disable=unsubscriptable-object
    ) -> None:
        try:
            _httpclient:MerossHttpClient = getattr(self, '_httpclient', None)
            if _httpclient is None:
                _httpclient = MerossHttpClient(host, key, async_get_clientsession(self.hass), LOGGER)
                self._httpclient = _httpclient
            else:
                _httpclient.host = host
                _httpclient.key = key

            response = await _httpclient.async_request(namespace, method, payload)
            r_header = response[mc.KEY_HEADER]
            if callback_or_device is not None:
                if isinstance(callback_or_device, MerossDevice):
                    callback_or_device.receive(r_header, response[mc.KEY_PAYLOAD])
                elif (r_header[mc.KEY_METHOD] == mc.METHOD_SETACK):
                    #we're actually only using this for SET->SETACK command confirmation
                    callback_or_device()

        except Exception as e:
            LOGGER.warning("MerossApi: error in async_http_request(%s)", str(e) or type(e).__name__)


    def request(self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: Union[dict, Optional[str]] = None, # pylint: disable=unsubscriptable-object
        host: str = None,
        callback_or_device: Union[Callable, MerossDevice] = None # pylint: disable=unsubscriptable-object
    ) -> None:
        """
        send a request with an 'adaptable protocol' behaviour i.e. use MQTT if the
        api is registered with the mqtt service else fallback to HTTP
        """
        #LOGGER.debug("MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)
        if (self.unsub_mqtt_subscribe is None) or (device_id is None):
            if host is None:
                if device_id is None:
                    LOGGER.warning("MerossApi: cannot call async_http_request (missing device_id and host)")
                    return
                device = self.devices.get(device_id)
                if device is None:
                    LOGGER.warning("MerossApi: cannot call async_http_request (device_id(%s) not found)", device_id)
                    return
                host = device.host
            self.hass.async_create_task(
                self.async_http_request(host, namespace, method, payload, key, callback_or_device)
            )
        else:
            self.mqtt_publish(device_id, namespace, method, payload, key)


    def update_polling_period(self) -> None:
        """
        called whenever a new device is added or a config_entry changes
        """
        polling_period = CONF_POLLING_PERIOD_DEFAULT
        for device in self.devices.values():
            if device.polling_period < polling_period:
                polling_period = device.polling_period
        self.coordinator.update_interval = timedelta(seconds=polling_period)


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.key = config_entry.data.get(CONF_KEY) or ''


    @callback
    def discovery_callback(self, _now: datetime):
        """
        async task to keep alive the discovery process:
        activated when any device is initially detected
        this task is not renewed when the list of devices
        under 'discovery' is empty or these became stale
        """
        self.unsub_discovery_callback = None
        if len(discovering := self.discovering) == 0:
            return

        _mqtt_is_connected = mqtt_is_connected(self.hass)
        epoch = time()
        for device_id, discovered in discovering.copy().items():
            if (epoch - discovered.get(MerossApi.KEY_STARTTIME, 0)) > PARAM_HEARTBEAT_PERIOD:
                # stale entry...remove
                discovering.pop(device_id)
                continue
            if (
                _mqtt_is_connected and
                ((epoch - discovered.get(MerossApi.KEY_REQUESTTIME, 0)) > PARAM_UNAVAILABILITY_TIMEOUT)
            ):
                if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                    self.mqtt_publish_get(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, self.key)
                else:
                    self.mqtt_publish_get(device_id, mc.NS_APPLIANCE_SYSTEM_ABILITY, self.key)
                discovered[MerossApi.KEY_REQUESTTIME] = epoch

        if len(discovering):
            self.unsub_discovery_callback = async_track_point_in_utc_time(
                self.hass,
                self.discovery_callback,
                datetime.fromtimestamp(epoch + PARAM_UNAVAILABILITY_TIMEOUT + 2)
            )



async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Meross IoT local LAN component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    LOGGER.debug("async_setup_entry entry_id = %s", entry.entry_id)
    api = MerossApi.get(hass)
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
        if not api.mqtt_registered:
            raise ConfigEntryNotReady("MQTT unavailable")

    if device_id is None:
        # this is the MQTT Hub entry
        api.key = entry.data.get(CONF_KEY) or ''
        api.unsub_entry_update_listener = entry.add_update_listener(api.entry_update_listener)
    else:
        #device related entry
        LOGGER.debug("async_setup_entry device_id = %s", device_id)
        cloud_key = entry.data.get(CONF_CLOUD_KEY)
        if cloud_key is not None:
            api.cloud_key = cloud_key # last loaded overwrites existing: shouldnt it be the same ?!
        device = api.build_device(device_id, entry)
        device.unsub_entry_update_listener = entry.add_update_listener(device.entry_update_listener)
        device.unsub_updatecoordinator_listener = api.coordinator.async_add_listener(device.updatecoordinator_listener)
        # this api is too recent (around April 2021): hass.config_entries.async_setup_platforms(entry, device.platforms.keys())
        for platform in device.platforms.keys():
            hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, platform))


    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.debug("async_unload_entry entry_id = %s", entry.entry_id)
    if (api := MerossApi.peek(hass)) is not None:
        device_id = entry.data.get(CONF_DEVICE_ID)
        if device_id is not None:
            LOGGER.debug("async_unload_entry device_id = %s", device_id)
            # when removing devices we could also need to cleanup platforms
            device = api.devices[device_id]
            if not await hass.config_entries.async_unload_platforms(entry, device.platforms.keys()):
                return False
            if device.unsub_entry_update_listener is not None:
                device.unsub_entry_update_listener()
                device.unsub_entry_update_listener = None
            if device.unsub_updatecoordinator_listener is not None:
                device.unsub_updatecoordinator_listener()
                device.unsub_updatecoordinator_listener = None
            api.devices.pop(device_id)
            device.shutdown()

        #don't cleanup: the MerossApi is still needed to detect MQTT discoveries
        #if (not api.devices) and (len(hass.config_entries.async_entries(DOMAIN)) == 1):
        #    api.shutdown()

    return True
