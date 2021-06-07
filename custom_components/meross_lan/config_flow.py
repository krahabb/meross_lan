"""Config flow for Meross IoT local LAN integration."""

from math import fabs
from homeassistant.components.mqtt import DATA_MQTT
import voluptuous as vol
from typing import OrderedDict, Optional
import json

from homeassistant import config_entries, core, exceptions
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .merossclient import MerossHttpClient, MerossDeviceDescriptor, const as mc

from .logger import LOGGER
from .const import (
    DOMAIN,
    CONF_HOST, CONF_DEVICE_ID, CONF_KEY,
    CONF_PAYLOAD, CONF_DEVICE_TYPE,
    CONF_PROTOCOL, CONF_PROTOCOL_OPTIONS,
)


def _mqtt_is_loaded(hass) -> bool:
    return hass.data.get(DATA_MQTT) is not None



class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info = None
    _device_id = None

    VERSION = 1
    # TODO pick one of the available connection classes in homeassistant/config_entries.py
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL


    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)


    async def async_step_user(self, user_input=None):
        # check we already configured the hub ..
        if (DOMAIN not in self._async_current_ids()) and _mqtt_is_loaded(self.hass):
            return await self.async_step_hub()

        errors = {}
        host = ""
        key = None
        if user_input is not None:
            host = user_input[CONF_HOST]
            key = user_input.get(CONF_KEY)
            try:
                client = MerossHttpClient(host, key, async_get_clientsession(self.hass), LOGGER)
                payload = (await client.async_request(mc.NS_APPLIANCE_SYSTEM_ALL))\
                    .get(mc.KEY_PAYLOAD)
                payload.update(
                    (await client.async_request(mc.NS_APPLIANCE_SYSTEM_ABILITY))\
                        .get(mc.KEY_PAYLOAD)
                    )
                discovery_info={
                    CONF_HOST: host,
                    CONF_PAYLOAD: payload,
                    CONF_KEY: key
                }
                return await self.async_step_discovery(discovery_info)
            except Exception as e:
                LOGGER.debug("Error connecting to meross appliance (%s)", str(e))
                errors["base"] = "cannot_connect"


        config_schema = {
            vol.Required(CONF_HOST, description={"suggested_value": host}): str,
            vol.Optional(CONF_KEY, description={"suggested_value": key}): str,
            }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(config_schema), errors=errors)


    async def async_step_hub(self, user_input=None):
        #right now this is only used to setup MQTT Hub feature to allow discovery

        if user_input == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            config_schema = { vol.Optional(CONF_KEY): str }

            return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))

        return self.async_create_entry(title="MQTT Hub", data=user_input)


    async def async_step_discovery(self, discovery_info: DiscoveryInfoType):
        self._discovery_info = discovery_info
        self._descriptor = MerossDeviceDescriptor(discovery_info.get(CONF_PAYLOAD, {}))
        self._device_id = self._descriptor.uuid
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        if CONF_DEVICE_ID not in discovery_info:
            discovery_info[CONF_DEVICE_ID] = self._device_id

        return await self.async_step_device()


    async def async_step_device(self, user_input=None):
        data = self._discovery_info

        if user_input is None:
            placeholders = {
                CONF_DEVICE_TYPE: self._descriptor.type,
                CONF_DEVICE_ID: self._descriptor.uuid,
                CONF_PAYLOAD: json.dumps(data.get(CONF_PAYLOAD, {}))
                }
            self.context["title_placeholders"] = placeholders
            config_schema = {}
            return self.async_show_form(
                step_id="device",
                data_schema=vol.Schema(config_schema),
                description_placeholders=placeholders
            )

        return self.async_create_entry(title=self._descriptor.type + " " + self._device_id, data=data)


    async def async_step_dhcp(self, discovery_info: DiscoveryInfoType):
        """Handle a flow initialized by DHCP discovery."""
        LOGGER.debug("received dhcp discovery: %s", json.dumps(discovery_info))

        return self.async_abort(reason='none')
        #return await self.async_step_discovery(discovery_info)



class OptionsFlowHandler(config_entries.OptionsFlow):
    """
        Manage device options configuration
    """

    def __init__(self, config_entry):
        self._config_entry = config_entry


    async def async_step_init(self, user_input=None):
        if self._config_entry.unique_id == DOMAIN:
            return await self.async_step_hub(user_input)
        return await self.async_step_device(user_input)


    async def async_step_hub(self, user_input=None):

        if user_input is not None:
            data = dict(self._config_entry.data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(title="", data=None)

        config_schema = OrderedDict()
        config_schema[
            vol.Optional(
                CONF_KEY,
                description={ "suggested_value" : self._config_entry.data.get(CONF_KEY) }
                )
            ] = str

        return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))


    async def async_step_device(self, user_input=None):
        data = self._config_entry.data
        descriptor = MerossDeviceDescriptor(data.get(CONF_PAYLOAD, {}))

        device_id = descriptor.uuid
        device_type = descriptor.type

        if user_input is not None:
            data = dict(data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            data[CONF_PROTOCOL] = user_input.get(CONF_PROTOCOL)
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(title=None, data=None)

        config_schema = OrderedDict()
        config_schema[
            vol.Optional(
                CONF_KEY,
                description={"suggested_value": data.get(CONF_KEY)}
                )
            ] = str
        config_schema[
            vol.Optional(
                CONF_PROTOCOL,
                description={"suggested_value": data.get(CONF_PROTOCOL)}
                )
            ] = vol.In(CONF_PROTOCOL_OPTIONS)

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders={
                CONF_DEVICE_TYPE: device_type,
                CONF_DEVICE_ID: device_id,
                CONF_PAYLOAD: json.dumps(data.get(CONF_PAYLOAD, {}))
            }
        )
