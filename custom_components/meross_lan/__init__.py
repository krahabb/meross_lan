"""The Meross IoT local LAN integration."""
from typing import Callable, Dict, Optional, Union
from time import time
import datetime
from json import (
    dumps as json_dumps,
    loads as json_loads,
)
from aiohttp.client_exceptions import ClientConnectionError

from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import mqtt
from homeassistant.helpers import device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)
from homeassistant.exceptions import ConfigEntryNotReady

from . import merossclient
from .merossclient import KeyType, MerossDeviceDescriptor, MerossHttpClient, const as mc

from logging import WARNING, INFO
from .logger import LOGGER, LOGGER_trap


from .meross_device import MerossDevice
from .meross_device_switch import MerossDeviceSwitch
from .meross_device_bulb import MerossDeviceBulb
from .meross_device_hub import MerossDeviceHub

from .const import (
    CONF_POLLING_PERIOD_DEFAULT, DOMAIN, SERVICE_REQUEST,
    CONF_HOST, CONF_OPTION_MQTT, CONF_PROTOCOL,
    CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD,
    DISCOVERY_TOPIC, REQUEST_TOPIC, RESPONSE_TOPIC,
    PARAM_UNAVAILABILITY_TIMEOUT,
)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Meross IoT local LAN component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    LOGGER.debug("async_setup_entry entry_id = %s", entry.entry_id)
    api = hass.data.get(DOMAIN)
    if api == None:
        api = MerossApi(hass)
        hass.data[DOMAIN] = api

    device_id = entry.data.get(CONF_DEVICE_ID)
    if (api.unsub_mqtt is None) and \
        ((device_id is None) or (entry.data.get(CONF_PROTOCOL) == CONF_OPTION_MQTT)):
        # this is the MQTT Hub entry or a device which needs MQTT
        # and we still havent registered MQTT
        try:
            await api.async_mqtt_register()
        except Exception as e:
            raise ConfigEntryNotReady from e

    if device_id is None:
        # this is the MQTT Hub entry
        api.key = entry.data.get(CONF_KEY)  # could be 'None' : if so defaults to "" but allows key reply trick
        api.unsub_entry_update_listener = entry.add_update_listener(api.entry_update_listener)
    else:
        #device related entry
        LOGGER.debug("async_setup_entry device_id = %s", device_id)
        device = api.build_device(device_id, entry)
        device.unsub_entry_update_listener = entry.add_update_listener(device.entry_update_listener)
        device.unsub_updatecoordinator_listener = api.coordinator.async_add_listener(device.updatecoordinator_listener)
        hass.config_entries.async_setup_platforms(entry, device.platforms.keys())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.debug("async_unload_entry entry_id = %s", entry.entry_id)
    api: MerossApi = hass.data.get(DOMAIN)
    if api is not None:

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

        #when removing the last configentry do a complete cleanup
        if (not api.devices) and (len(hass.config_entries.async_entries(DOMAIN)) == 1):
            if api.unsub_mqtt is not None:
                api.unsub_mqtt()
                api.unsub_mqtt = None
            if api.unsub_entry_update_listener is not None:
                api.unsub_entry_update_listener()
                api.unsub_entry_update_listener = None
            if api.unsub_updatecoordinator_listener is not None:
                api.unsub_updatecoordinator_listener()
                api.unsub_updatecoordinator_listener = None
            hass.data.pop(DOMAIN)

    return True


class MerossApi:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.key = None
        self.devices: Dict[str, MerossDevice] = {}
        self.discovering: Dict[str, dict] = {}
        self.unsub_mqtt = None
        self.unsub_entry_update_listener = None
        self.unsub_updatecoordinator_listener = None

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
            update_interval=datetime.timedelta(seconds=CONF_POLLING_PERIOD_DEFAULT),
        )

        def _request(service_call):
            self.request(
                device_id=service_call.data.get(CONF_DEVICE_ID),
                namespace=service_call.data.get(mc.KEY_NAMESPACE),
                method=service_call.data.get(mc.KEY_METHOD),
                payload=json_loads(service_call.data.get(mc.KEY_PAYLOAD, "{}")),
                key=service_call.data.get(CONF_KEY, self.key),
                host=service_call.data.get(CONF_HOST)
            )
            return
        hass.services.async_register(DOMAIN, SERVICE_REQUEST, _request)

        return


    async def async_mqtt_register(self):
        # Listen to a message on MQTT.
        @callback
        async def mqtt_receive(msg):
            try:
                device_id = msg.topic.split("/")[2]
                mqttpayload = json_loads(msg.payload)
                header = mqttpayload.get(mc.KEY_HEADER)
                method = header.get(mc.KEY_METHOD)
                namespace = header.get(mc.KEY_NAMESPACE)
                payload = mqttpayload.get(mc.KEY_PAYLOAD)

                LOGGER.debug("MerossApi: MQTT RECV device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)

                device = self.devices.get(device_id)
                if device == None:
                    # lookout for any disabled/ignored entry
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
                            LOGGER_trap(INFO, 300, "Ignoring discovery for device_id: %s (ConfigEntry is %s)", device_id, msg_reason)
                            return
                    #also skip discovered integrations waititng in HA queue
                    for flow in self.hass.config_entries.flow.async_progress():
                        if (flow.get("handler") == DOMAIN) and (flow.get("context", {}).get("unique_id") == device_id):
                            LOGGER_trap(INFO, 300, "Ignoring discovery for device_id: %s (ConfigEntry is in progress)", device_id)
                            return

                    replykey = merossclient.get_replykey(header, self.key)
                    if replykey != self.key:
                        LOGGER_trap(WARNING, 300, "Meross discovery key error for device_id: %s", device_id)
                        if self.key is not None:# we're using a fixed key in discovery so ignore this device
                            return

                    discovered = self.discovering.get(device_id)
                    if discovered == None:
                        # new device discovered: try to determine the capabilities
                        self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GET, key=replykey)
                        self.discovering[device_id] = { "__time": time() }
                        if self.unsub_updatecoordinator_listener is None:
                            self.unsub_updatecoordinator_listener = self.coordinator.async_add_listener(self.updatecoordinator_listener)

                    else:
                        if method == mc.METHOD_GETACK:
                            if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
                                discovered[mc.NS_APPLIANCE_SYSTEM_ALL] = payload
                                self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.METHOD_GET, key=replykey)
                                discovered["__time"] = time()
                                return
                            elif namespace == mc.NS_APPLIANCE_SYSTEM_ABILITY:
                                if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                                    self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GET, key=replykey)
                                    discovered["__time"] = time()
                                    return
                                payload.update(discovered[mc.NS_APPLIANCE_SYSTEM_ALL])
                                self.discovering.pop(device_id)
                                if (len(self.discovering) == 0) and self.unsub_updatecoordinator_listener:
                                    self.unsub_updatecoordinator_listener()
                                    self.unsub_updatecoordinator_listener = None
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
                        #we might get here from spurious PUSH or something sent from the device
                        #check for timeout and eventually reset the procedure
                        if (time() - discovered.get("__time", 0)) > PARAM_UNAVAILABILITY_TIMEOUT:
                            if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                                self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GET, key=replykey)
                            else:
                                self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.METHOD_GET, key=replykey)
                            discovered["__time"] = time()
                            return

                else:
                    device.mqtt_receive(namespace, method, payload, merossclient.get_replykey(header, device.key))

            except Exception as e:
                LOGGER.debug("MerossApi: mqtt_receive exception:(%s) payload:(%s)", str(e), msg.payload)

            return

        self.unsub_mqtt = await self.hass.components.mqtt.async_subscribe(
            DISCOVERY_TOPIC, mqtt_receive
        )


    def build_device(self, device_id: str, entry: ConfigEntry) -> MerossDevice:
        """
        scans device descriptor to build a 'slightly' specialized MerossDevice
        The base MerossDevice class is a bulk 'do it all' implementation
        but some devices (i.e. Hub) need a (radically?) different behaviour
        """
        descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD, {}))
        if not descriptor.digest: # legacy firmware -> switches likely
            device = MerossDeviceSwitch(self, descriptor, entry)
        elif (mc.KEY_HUB in descriptor.digest):
            device = MerossDeviceHub(self, descriptor, entry)
        elif (mc.KEY_LIGHT in descriptor.digest):
            device = MerossDeviceBulb(self, descriptor, entry)
        else:
            device = MerossDeviceSwitch(self, descriptor, entry)

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
        payload: dict = {},
        key: KeyType = None
    ) -> None:
        LOGGER.debug("MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)
        self.hass.components.mqtt.async_publish(
            REQUEST_TOPIC.format(device_id),
            json_dumps(merossclient.build_payload(
                namespace, method, payload,
                key, _from=RESPONSE_TOPIC.format(device_id))),
            0,
            False)


    async def async_http_request(self,
        host: str,
        namespace: str,
        method: str,
        payload: dict = {},
        key: KeyType = None,
        callback_or_device: Union[Callable, MerossDevice] = None # pylint: disable=unsubscriptable-object
    ) -> None:
        try:
            _httpclient:MerossHttpClient = getattr(self, '_httpclient', None)
            if _httpclient is None:
                _httpclient = MerossHttpClient(host, key, async_get_clientsession(self.hass), LOGGER)
                self._httpclient = _httpclient
            else:
                _httpclient.set_host(host)
                _httpclient.key = key

            response = await _httpclient.async_request(namespace, method, payload)
            r_header = response[mc.KEY_HEADER]
            r_namespace = r_header[mc.KEY_NAMESPACE]
            r_method = r_header[mc.KEY_METHOD]
            if callback_or_device is not None:
                if isinstance(callback_or_device, MerossDevice):
                    callback_or_device.receive( r_namespace, r_method,
                        response[mc.KEY_PAYLOAD], _httpclient.replykey)
                elif (r_method == mc.METHOD_SETACK):
                    #we're actually only using this for SET->SETACK command confirmation
                    callback_or_device()

        except ClientConnectionError as e:
            LOGGER.info("MerossApi: client connection error in async_http_request(%s)", str(e))
        except Exception as e:
            LOGGER.warning("MerossApi: error in async_http_request(%s)", str(e))


    def request(self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict = {},
        key: Union[dict, Optional[str]] = None, # pylint: disable=unsubscriptable-object
        host: str = None,
        callback_or_device: Union[Callable, MerossDevice] = None # pylint: disable=unsubscriptable-object
    ) -> None:
        """
        send a request with an 'adaptable protocol' behaviour i.e. use MQTT if the
        api is registered with the mqtt service else fallback to HTTP
        """
        #LOGGER.debug("MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)
        if (self.unsub_mqtt is None) or (device_id is None):
            if host is None:
                if device_id is None:
                    LOGGER.warning("MerossApi: cannot call async_http_request (missing device_id and host)")
                    return
                device = self.devices.get(device_id)
                if device is None:
                    LOGGER.warning("MerossApi: cannot call async_http_request (device_id(%s) not found)", device_id)
                    return
                host = device.descriptor.ipAddress
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
        self.coordinator.update_interval = datetime.timedelta(seconds=polling_period)


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.key = config_entry.data.get(CONF_KEY)


    @callback
    def updatecoordinator_listener(self) -> None:
        """
        called by DataUpdateCoordinator when we have pending discoveries
        this callback gets attached/detached dinamically when we have discoveries pending
        """
        now = time()
        for device_id, discovered in self.discovering.items():
            if (now - discovered.get("__time", 0)) > PARAM_UNAVAILABILITY_TIMEOUT:
                if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                    self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GET, {}, self.key)
                else:
                    self.mqtt_publish(device_id, mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.METHOD_GET, {}, self.key)
                discovered["__time"] = now

        return
