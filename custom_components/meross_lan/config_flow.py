"""Config flow for Meross IoT local LAN integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries, core, exceptions

from .const import *  # pylint:disable=unused-import


_LOGGER = logging.getLogger(__name__)

# TODO adjust the data schema to the data that you need
# STEP_USER_DATA_SCHEMA = vol.Schema({CONF_DEVICE_ID: str})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info = None

    VERSION = 1
    # TODO pick one of the available connection classes in homeassistant/config_entries.py
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    async def async_step_user(self, user_input=None):
        #if self._async_in_progress() or self._async_current_entries():
        #    return self.async_abort(reason="single_instance_allowed")
        if self._discovery_info == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="MQTT Hub", data={})

        return self.async_create_entry(title=self.unique_id, data=self._discovery_info)

        #if user_input is None:
        #    return self.async_show_form(step_id="user", data_schema=vol.Schema({ vol.Required(CONF_DEVICE_ID): vol.All(str, vol.Length(min=32, max=32))}))

        #return self.async_create_entry(title=DOMAIN, data=user_input)

    async def async_step_discovery(self, info):
        await self.async_set_unique_id(info[CONF_DEVICE_ID])
        self._abort_if_unique_id_configured()
        self._discovery_info = info
        return self.async_show_form(step_id="user", data_schema=vol.Schema({ vol.Required(CONF_DEVICE_ID, default=info[CONF_DEVICE_ID]): vol.All(str, vol.Length(min=32, max=32))}))
        #return self.async_create_entry(title=DOMAIN, data=info)

    async def deleted_async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({CONF_DEVICE_ID: self._device_id}),
            )

        errors = {}

        self._device_id = user_input[CONF_DEVICE_ID]

        if len(self._device_id) == 32:
            await self.async_set_unique_id(f"{DOMAIN}-{self._device_id}")
            return self.async_create_entry(title=self.unique_id, data=user_input)
        else:
            errors["base"] = "invalid_device_id"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({CONF_DEVICE_ID: self._device_id}),
                errors=errors,
            )

    async def deleted_async_step_mqtt(self, discovery_info=None):
        """Handle a flow initialized by MQTT discovery."""

        values = discovery_info.topic.split("/")
        self._device_id = values[2]
        await self.async_set_unique_id(f"{DOMAIN}-{self._device_id}")

        # return await self.async_step_confirm()
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({CONF_DEVICE_ID: self._device_id})
        )
