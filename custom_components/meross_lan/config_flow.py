"""Config flow for Meross IoT local LAN integration."""

import voluptuous as vol

from homeassistant import config_entries, core, exceptions

from .const import *  # pylint:disable=unused-import


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info = None

    VERSION = 1
    # TODO pick one of the available connection classes in homeassistant/config_entries.py
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    """
    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)
    """

    async def async_step_user(self, user_input=None):
        #if self._async_in_progress() or self._async_current_entries():
        #    return self.async_abort(reason="single_instance_allowed")
        if self._discovery_info == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="MQTT Hub", data={})

        return self.async_create_entry(title=self.unique_id, data=self._discovery_info)


    async def async_step_discovery(self, info):
        self._device_id = info[CONF_DEVICE_ID]
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()
        self._discovery_info = info

        config_schema = vol.Schema({
            vol.Required(CONF_DEVICE_ID, default=self._device_id): vol.All(str, vol.Length(min=32, max=32))
            })

        return self.async_show_form(step_id="config", data_schema=config_schema)

    async def async_step_config(self, user_input=None):
        return self.async_create_entry(title=self.unique_id, data=self._discovery_info)



"""
Manage device options configuration
This code is actually disabled since I prefer to not add too many configuration tweaks at the moment.
The initial implementation was about letting the user choose if they wanted specific sensors for power values or not
Actually, I think the default solution of removing attributes (from switches) and adding specific sensors is 'plain right':
As an HA user you can just disable unwanted entities or remove them from recorder if they pollute your history
class OptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        discoverypayload = self._config_entry.data.get(CONF_DISCOVERY_PAYLOAD, {})
        ability = discoverypayload.get("ability", {})
        options = self._config_entry.options or {}

        device_id = self._config_entry.data.get(CONF_DEVICE_ID)
        device_name = discoverypayload.get("all", {}).get("system", {}).get("hardware", {}).get("type", "Meross") + " " + device_id

        config_schema = OrderedDict()

        if NS_APPLIANCE_CONTROL_ELECTRICITY in ability:
            config_schema[vol.Optional(CONF_OPTION_SENSOR_POWER, default=options.get(CONF_OPTION_SENSOR_POWER, False))] = bool
            config_schema[vol.Optional(CONF_OPTION_SENSOR_CURRENT, default=options.get(CONF_OPTION_SENSOR_CURRENT, False))] = bool
            config_schema[vol.Optional(CONF_OPTION_SENSOR_VOLTAGE, default=options.get(CONF_OPTION_SENSOR_VOLTAGE, False))] = bool

        if NS_APPLIANCE_CONTROL_CONSUMPTIONX in ability:
            config_schema[vol.Optional(CONF_OPTION_SENSOR_ENERGY, default=options.get(CONF_OPTION_SENSOR_ENERGY, False))] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(config_schema),
            description_placeholders={"device_name" : device_name}
        )
        """