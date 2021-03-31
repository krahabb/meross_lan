"""Config flow for Meross IoT local LAN integration."""

import voluptuous as vol
from typing import OrderedDict, Optional
import json

from homeassistant import config_entries, core, exceptions

from .const import (
    DOMAIN,
    CONF_DEVICE_ID, CONF_KEY, CONF_DISCOVERY_PAYLOAD, CONF_DEVICE_TYPE,
    MANUFACTURER
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info = None
    _device_id = None
    _device_type = None

    VERSION = 1
    # TODO pick one of the available connection classes in homeassistant/config_entries.py
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH


    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)


    async def async_step_user(self, user_input=None):

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        config_schema = OrderedDict()
        config_schema[vol.Optional(CONF_KEY)] = str

        return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))

    async def async_step_hub(self, user_input=None):
        #right now this is only used to setup MQTT Hub feature to allow discovery
        """
        if user_input == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            config_schema = OrderedDict()
            config_schema[vol.Optional(CONF_KEY)] = str

            return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))
        """
        return self.async_create_entry(title="MQTT Hub", data=user_input)


    async def async_step_discovery(self, info):
        self._device_id = info[CONF_DEVICE_ID]
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        self._discovery_info = info
        self._device_type = info.get(CONF_DISCOVERY_PAYLOAD, {}).get("all", {}).get("system", {}).get("hardware", {}).get("type", MANUFACTURER)

        self.context["title_placeholders"] = {
            CONF_DEVICE_TYPE: self._device_type,
            CONF_DEVICE_ID: self._device_id
        }
        #config_schema = OrderedDict()
        #config_schema[vol.Optional(CONF_KEY, description={ "suggested_value" : info.get(CONF_KEY) })] = str

        #return self.async_show_form(step_id="device", data_schema=vol.Schema(config_schema))
        return self.async_show_form(step_id="device")

    async def async_step_device(self, user_input=None):
        data = self._discovery_info
        #device_id = data.get(CONF_DEVICE_ID)
        discoverypayload = data.get(CONF_DISCOVERY_PAYLOAD, {})
        #all = discoverypayload.get("all", {})
        #device_type = all.get("system", {}).get("hardware", {}).get("type", MANUFACTURER)

        if user_input is None:
            config_schema = OrderedDict()
            #config_schema[vol.Optional(CONF_KEY, description={ "suggested_value" : data.get(CONF_KEY) })] = str
            return self.async_show_form(
                step_id="device",
                data_schema=vol.Schema(config_schema),
                description_placeholders={
                    "device_type": self._device_type,
                    "device_id": self._device_id,
                    "payload": json.dumps(discoverypayload)
                }
            )

        # not configuring key here since it should be right from discovery ;)
        #data[CONF_KEY] = user_input.get(CONF_KEY)
        return self.async_create_entry(title=self._device_type + " " + self._device_id, data=data)



"""
Manage device options configuration
This code is actually disabled since I prefer to not add too many configuration tweaks at the moment.
The initial implementation was about letting the user choose if they wanted specific sensors for power values or not
Actually, I think the default solution of removing attributes (from switches) and adding specific sensors is 'plain right':
As an HA user you can just disable unwanted entities or remove them from recorder if they pollute your history
"""
class OptionsFlowHandler(config_entries.OptionsFlow):

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
        config_schema[vol.Optional(CONF_KEY, description={ "suggested_value" : self._config_entry.data.get(CONF_KEY) } )] = str

        return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))

    async def async_step_device(self, user_input=None):
        data = self._config_entry.data
        discoverypayload = data.get(CONF_DISCOVERY_PAYLOAD, {})
        #ability = discoverypayload.get("ability", {})
        all = discoverypayload.get("all", {})
        device_id = data.get(CONF_DEVICE_ID)
        device_type = all.get("system", {}).get("hardware", {}).get("type", MANUFACTURER)

        if user_input is not None:
            data = dict(data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            """
            device_id = user_input[CONF_DEVICE_ID]

            all = json.loads(user_input["all"])
            data = {
                CONF_DEVICE_ID: device_id,
                CONF_KEY: user_input[CONF_KEY],
                CONF_DISCOVERY_PAYLOAD: {
                    "all": all,
                    "ability": json.loads(user_input["ability"])
                },
            }
            device_name = all.get("system", {}).get("hardware", {}).get("type", "Meross") + " " + device_id
            """
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(title=None, data=None)

        """
        data = self._config_entry.data or {}
        discoverypayload = data.get(CONF_DISCOVERY_PAYLOAD, {})
        ability = discoverypayload.get("ability", {})
        all = discoverypayload.get("all", {})

        device_id = data.get(CONF_DEVICE_ID)
        device_name = all.get("system", {}).get("hardware", {}).get("type", "Meross") + " " + device_id
        """

        config_schema = OrderedDict()
        #config_schema[vol.Required(CONF_DEVICE_ID, default=device_id)] = vol.All(str, vol.Length(min=32, max=32))
        config_schema[vol.Optional(CONF_KEY, description={ "suggested_value" : data.get(CONF_KEY) } )] = str
        #config_schema[vol.Optional("ability", default=json.dumps(ability, indent=4))] = str
        #config_schema[vol.Optional("all", default=json.dumps(all, indent = 4))] = str

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders={
                "device_type": device_type,
                "device_id": device_id,
                "payload": json.dumps(discoverypayload)
            }
        )
