"""The Meross IoT local LAN integration."""
from typing import Any, Callable, Dict, List, Optional, Union
import asyncio
from time import time
import datetime
from uuid import uuid4
from hashlib import md5
from json import (
    dumps as json_dumps,
    loads as json_loads,
)



from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import mqtt
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .logger import LOGGER, LOGGER_trap
from logging import WARNING, INFO

from .meross_device import MerossDevice
from .const import *


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Meross IoT local LAN component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""
    LOGGER.debug("async_setup_entry entry_id = %s", entry.entry_id)
    api = hass.data.get(DOMAIN)
    if api == None:
        try:
            api = MerossApi(hass)
            hass.data[DOMAIN] = api
            await api.async_mqtt_register()

            def _mqtt_publish(service_call):
                device_id = service_call.data.get(CONF_DEVICE_ID)
                method = service_call.data.get("method")
                namespace = service_call.data.get("namespace")
                key = service_call.data.get(CONF_KEY, api.key)
                payload = service_call.data.get("payload", "{}")
                api.mqtt_publish(device_id, namespace, method, json_loads(payload), key)
                return
            hass.services.async_register(DOMAIN, SERVICE_MQTT_PUBLISH, _mqtt_publish)
        except:
            return False

    device_id = entry.data.get(CONF_DEVICE_ID)
    if device_id == None:
        # this is the MQTT Hub entry
        api.key = entry.data.get(CONF_KEY)  # could be 'None' : if so defaults to "" but allows key reply trick
        api.unsub_entry_update_listener = entry.add_update_listener(api.entry_update_listener)
    else:
        #device related entry
        LOGGER.debug("async_setup_entry device_id = %s", device_id)
        device = MerossDevice(api, device_id, entry)
        api.devices[device_id] = device

        p_system = entry.data.get(CONF_DISCOVERY_PAYLOAD, {}).get("all", {}).get("system", {})
        p_hardware = p_system.get("hardware", {})
        p_firmware = p_system.get("firmware", {})
        p_hardware_type = p_hardware.get("type", MANUFACTURER)

        try:
            #use newer api
            device_registry.async_get(hass).async_get_or_create(
                config_entry_id = entry.entry_id,
                connections = {(device_registry.CONNECTION_NETWORK_MAC, p_hardware.get("macAddress"))},
                identifiers = {(DOMAIN, device_id)},
                manufacturer = MANUFACTURER,
                name = p_hardware_type + " " + device_id,
                model = p_hardware_type + " " + p_hardware.get("version", ""),
                sw_version = p_firmware.get("version"),
            )
        except:
            #fallback: as of 27-03-2021 this is still working
            device_registry.async_get_registry(hass).async_get_or_create(
                config_entry_id = entry.entry_id,
                connections = {(device_registry.CONNECTION_NETWORK_MAC, p_hardware.get("macAddress"))},
                identifiers = {(DOMAIN, device_id)},
                manufacturer = MANUFACTURER,
                name = p_hardware_type + " " + device_id,
                model = p_hardware_type + " " + p_hardware.get("version", ""),
                sw_version = p_firmware.get("version"),
            )


        if device.has_switches:
            hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, "switch"))
        if device.has_lights:
            hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, "light"))
        if device.has_sensors:
            hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, "sensor"))
        if device.has_covers:
            hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, "cover"))

        device.unsub_entry_update_listener = entry.add_update_listener(device_entry_update_listener)
        device.unsub_updatecoordinator_listener = api.coordinator.async_add_listener(device.updatecoordinator_listener)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.debug("async_unload_entry entry_id = %s", entry.entry_id)
    api = hass.data.get(DOMAIN)
    if api != None:

        device_id = entry.data.get(CONF_DEVICE_ID)
        if device_id != None:
            LOGGER.debug("async_unload_entry device_id = %s", device_id)
            # when removing devices we could also need to cleanup platforms
            device = api.devices[device_id]
            platforms_unload = []
            if device.has_switches:
                platforms_unload.append(hass.config_entries.async_forward_entry_unload(entry, "switch"))
            if device.has_lights:
                platforms_unload.append(hass.config_entries.async_forward_entry_unload(entry, "light"))
            if device.has_sensors:
                platforms_unload.append(hass.config_entries.async_forward_entry_unload(entry, "sensor"))
            if device.has_covers:
                platforms_unload.append(hass.config_entries.async_forward_entry_unload(entry, "cover"))

            if platforms_unload:
                if False == all(await asyncio.gather(*platforms_unload)):
                    return False

            if device.unsub_entry_update_listener:
                device.unsub_entry_update_listener()
                device.unsub_entry_update_listener = None
            if device.unsub_updatecoordinator_listener:
                device.unsub_updatecoordinator_listener()
                device.unsub_updatecoordinator_listener = None

            api.devices.pop(device_id)

        #when removing the last configentry do a complete cleanup
        if not (api.devices) and (len(hass.config_entries.async_entries(DOMAIN)) == 1):
            if api.unsub_mqtt:
                api.unsub_mqtt()
                api.unsub_mqtt = None
            if api.unsub_entry_update_listener:
                api.unsub_entry_update_listener()
                api.unsub_entry_update_listener = None
            if api.unsub_updatecoordinator_listener:
                api.unsub_updatecoordinator_listener()
                api.unsub_updatecoordinator_listener = None
            hass.data.pop(DOMAIN)

    return True


async def device_entry_update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    await hass.config_entries.async_reload(config_entry.entry_id)


def build_payload(namespace: str, method: str, payload: dict = {}, key: Union[dict, Optional[str]] = None):  # pylint: disable=unsubscriptable-object
    if isinstance(key, dict):
        key["namespace"] = namespace
        key["method"] = method
        key["payloadVersion"] = 1
        key["from"] = ""
        return json_dumps({
            "header": key,
            "payload": payload
        })
    else:
        messageid = uuid4().hex
        timestamp = int(time())
        return json_dumps({
            "header": {
                "messageId": messageid,
                "namespace": namespace,
                "method": method,
                "payloadVersion": 1,
                #"from": "/appliance/9109182170548290882048e1e9522946/publish",
                "timestamp": timestamp,
                "timestampMs": 0,
                "sign": md5((messageid + (key or "") + str(timestamp)).encode('utf-8')).hexdigest()
            },
            "payload": payload
        })



def get_replykey(header: dict, key: Optional[str] = None) -> Union[dict, Optional[str]]:  # pylint: disable=unsubscriptable-object
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    sign = md5((header["messageId"] + (key or "") + str(header["timestamp"])).encode('utf-8')).hexdigest()
    if sign == header["sign"]:
        return key

    return header

class MerossApi:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.key = None
        self.devices: Dict[str, MerossDevice] = {}
        self.discovering: Dict[str, {}] = {}
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
            update_interval=datetime.timedelta(seconds=PARAM_UPDATE_POLLING_PERIOD),
        )
        return


    async def async_mqtt_register(self):
        # Listen to a message on MQTT.
        @callback
        async def mqtt_receive(msg):
            try:
                device_id = msg.topic.split("/")[2]
                mqttpayload = json_loads(msg.payload)
                header = mqttpayload.get("header")
                method = header.get("method")
                namespace = header.get("namespace")
                payload = mqttpayload.get("payload")

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
                            LOGGER_trap(INFO, "Ignoring discovery for device_id: %s (ConfigEntry is %s)", device_id, msg_reason)
                            return
                    #also skip discovered integrations waititng in HA queue
                    for flow in self.hass.config_entries.flow.async_progress():
                        if (flow.get("handler") == DOMAIN) and (flow.get("context", {}).get("unique_id") == device_id):
                            LOGGER_trap(INFO, "Ignoring discovery for device_id: %s (ConfigEntry is in progress)", device_id)
                            return

                    replykey = get_replykey(header, self.key)
                    if replykey != self.key:
                        LOGGER_trap(WARNING, "Meross discovery key error for device_id: %s", device_id)
                        if self.key is not None:# we're using a fixed key in discovery so ignore this device
                            return

                    discovered = self.discovering.get(device_id)
                    if discovered == None:
                        # new device discovered: try to determine the capabilities
                        self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ALL, METHOD_GET, key=replykey)
                        self.discovering[device_id] = { "__time": time() }
                        if self.unsub_updatecoordinator_listener is None:
                            self.unsub_updatecoordinator_listener = self.coordinator.async_add_listener(self.updatecoordinator_listener)

                    else:
                        if method == METHOD_GETACK:
                            if namespace == NS_APPLIANCE_SYSTEM_ALL:
                                discovered[NS_APPLIANCE_SYSTEM_ALL] = payload
                                self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ABILITY, METHOD_GET, key=replykey)
                                discovered["__time"] = time()
                                return
                            elif namespace == NS_APPLIANCE_SYSTEM_ABILITY:
                                if discovered.get(NS_APPLIANCE_SYSTEM_ALL) is None:
                                    self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ALL, METHOD_GET, key=replykey)
                                    discovered["__time"] = time()
                                    return
                                payload.update(discovered[NS_APPLIANCE_SYSTEM_ALL])
                                self.discovering.pop(device_id)
                                if (len(self.discovering) == 0) and self.unsub_updatecoordinator_listener:
                                    self.unsub_updatecoordinator_listener()
                                    self.unsub_updatecoordinator_listener = None
                                await self.hass.config_entries.flow.async_init(
                                    DOMAIN,
                                    context={ "source": SOURCE_DISCOVERY },
                                    data={
                                        CONF_DEVICE_ID: device_id,
                                        CONF_DISCOVERY_PAYLOAD: payload,
                                        CONF_KEY: replykey
                                    },
                                )
                                return
                        #we might get here from spurious PUSH or something sent from the device
                        #check for timeout and eventually reset the procedure
                        if (time() - discovered.get("__time", 0)) > PARAM_UNAVAILABILITY_TIMEOUT:
                            if discovered.get(NS_APPLIANCE_SYSTEM_ALL) is None:
                                self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ALL, METHOD_GET, key=replykey)
                            else:
                                self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ABILITY, METHOD_GET, key=replykey)
                            discovered["__time"] = time()
                            return


                else:
                    device.parsepayload(namespace, method, payload, get_replykey(header, device.key))

            except Exception as e:
                LOGGER.debug("MerossApi: mqtt_receive exception:(%s) payload:(%s)", str(e), msg.payload)

            return

        self.unsub_mqtt = await self.hass.components.mqtt.async_subscribe(
            DISCOVERY_TOPIC, mqtt_receive
        )


    def mqtt_publish(self, device_id: str, namespace: str, method: str, payload: dict = {}, key: Union[dict, Optional[str]] = None):  # pylint: disable=unsubscriptable-object
        LOGGER.debug("MerossApi: MQTT SEND device_id:(%s) method:(%s) namespace:(%s)", device_id, method, namespace)
        mqttpayload = build_payload(namespace, method, payload, key)
        return self.hass.components.mqtt.async_publish(COMMAND_TOPIC.format(device_id), mqttpayload, 0, False)


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.key = config_entry.data.get(CONF_KEY)
        return


    @callback
    def updatecoordinator_listener(self) -> None:
        """
        called by DataUpdateCoordinator when we have pending discoveries
        this callback gets attached/detached dinamically when we have discoveries pending
        """
        now = time()
        for device_id, discovered in self.discovering.items():
            if (now - discovered.get("__time", 0)) > PARAM_UNAVAILABILITY_TIMEOUT:
                if discovered.get(NS_APPLIANCE_SYSTEM_ALL) is None:
                    self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ALL, METHOD_GET, {}, self.key)
                else:
                    self.mqtt_publish(device_id, NS_APPLIANCE_SYSTEM_ABILITY, METHOD_GET, {}, self.key)
                discovered["__time"] = now

        return
