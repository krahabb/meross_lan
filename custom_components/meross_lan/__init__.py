"""The Meross IoT local LAN integration."""
import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import mqtt
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .meross_device import build_payload, MerossDevice
from .const import *


_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Meross IoT local LAN component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meross IoT local LAN from a config entry."""

    credentials = {}
    api = hass.data.get(DOMAIN)
    if api == None:
        api = MerossLan(hass)
        hass.data[DOMAIN] = api
        # Listen to a message on MQTT.
        @callback
        async def message_received(msg):
            device_id = msg.topic.split("/")[2]
            mqttpayload = json.loads(msg.payload)
            header = mqttpayload.get("header")
            method = header.get("method")
            namespace = header.get("namespace")
            payload = mqttpayload.get("payload")
            credentials = {
                "messageId": header.get("messageId"),
                "timestamp": header.get("timestamp"),
                "sign": header.get("sign")
            }

            device = api.devices.get(device_id)
            if device == None:
                discovered = api.discovering.get(device_id)
                if discovered == None:
                    # new device discovered: try to determine the capabilities
                    api.discovering[device_id] = {}
                    mqttpayload = build_payload(NS_APPLIANCE_SYSTEM_ALL, METHOD_GET, {}, credentials)
                    hass.components.mqtt.async_publish(COMMAND_TOPIC.format(device_id), mqttpayload, 1, False)
                else:
                    if (method == METHOD_GETACK):

                        if (namespace == NS_APPLIANCE_SYSTEM_ALL):
                            discovered[NS_APPLIANCE_SYSTEM_ALL] = payload
                            mqttpayload = build_payload(NS_APPLIANCE_SYSTEM_ABILITY, METHOD_GET, {}, credentials)
                            hass.components.mqtt.async_publish(COMMAND_TOPIC.format(device_id), mqttpayload, 1, False)
                        elif (namespace == NS_APPLIANCE_SYSTEM_ABILITY):
                            payload.update(discovered[NS_APPLIANCE_SYSTEM_ALL])
                            api.discovering.pop(device_id)
                            await hass.config_entries.flow.async_init(
                                DOMAIN,
                                context={"source": SOURCE_DISCOVERY},
                                data={CONF_DEVICE_ID: device_id, CONF_DISCOVERY_PAYLOAD: payload, CONF_CREDENTIALS: credentials},
                            )

            else:
                device.parsepayload(namespace, method, payload, credentials)
            return

        api.unsubscribe_mqtt = await hass.components.mqtt.async_subscribe( DISCOVERY_TOPIC, message_received)

        async def async_update_data():
            for device in api.devices.values():
                device.triggerupdate()
            return None

        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=DOMAIN,
            update_method=async_update_data,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=30),
        )
        api.unsubscribe_coordinator = coordinator.async_add_listener(api._handle_coordinator_update)

    device_id = entry.data.get(CONF_DEVICE_ID)
    if device_id != None:
        discoverypayload = entry.data.get(CONF_DISCOVERY_PAYLOAD)
        credentials = entry.data.get(CONF_CREDENTIALS)
        device = MerossDevice(device_id, discoverypayload, credentials, hass.components.mqtt.async_publish)
        api.devices[device_id] = device

        p_system = discoverypayload.get("all", {}).get("system", {})
        p_hardware = p_system.get("hardware", {})
        p_firmware = p_system.get("firmware", {})
        from homeassistant.helpers import device_registry as dr
        device_registry = await dr.async_get_registry(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, p_firmware.get("wifiMac"))},
            identifiers={(DOMAIN, device_id)},
            manufacturer="Meross",
            name=p_hardware.get("type", "Meross") + " " + device_id,
            model=p_hardware.get("type"),
            sw_version=p_firmware.get("version"),
            )

        if (len(device.switches) > 0):
            hass.async_create_task(
                hass.config_entries.async_forward_entry_setup(entry, "switch")
            )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""

    api = hass.data.get(DOMAIN)
    if (api != None):

        device_id = entry.data.get(CONF_DEVICE_ID)
        if device_id != None:
            #when removing devices we could also need to cleanup platforms
            device = api.devices.pop(device_id)
            if (len(device.switches) > 0):
                await hass.config_entries.async_forward_entry_unload(entry, "switch")

        # if removing the last configentry do a complete cleanup
        if not(api.devices) and (len(hass.config_entries.async_entries(DOMAIN)) == 1):
            api.unsubscribe_mqtt()
            api.unsubscribe_coordinator()
            hass.data.pop(DOMAIN)

    return True


class MerossLan:
    def __init__(self, hass: HomeAssistant):
        self.devices: Dict[str, MerossDevice] = {}
        self.discovering: Dict[str, {}] = {}
        return

    @callback
    def _handle_coordinator_update(self) -> None:
        #called when coordinator runs....after calling async
        return #nothing to do atm

