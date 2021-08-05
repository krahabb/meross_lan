"""Config flow for Meross IoT local LAN integration."""
from time import time
import voluptuous as vol
from typing import OrderedDict
import json
try:
    from pytz import common_timezones
except Exception:
    common_timezones = None

from homeassistant import config_entries
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .merossclient import MerossHttpClient, MerossDeviceDescriptor, const as mc, get_productnametype

from .helpers import LOGGER, mqtt_is_loaded
from .const import (
    DOMAIN,
    CONF_HOST, CONF_DEVICE_ID, CONF_KEY,
    CONF_PAYLOAD, CONF_DEVICE_TYPE,
    CONF_PROTOCOL, CONF_PROTOCOL_OPTIONS,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT,
    CONF_TIME_ZONE,
    CONF_TRACE, CONF_TRACE_TIMEOUT,
)



async def _http_discovery(host: str, key: str, hass) -> dict:
    client = MerossHttpClient(host, key, async_get_clientsession(hass), LOGGER)
    payload = (await client.async_request(mc.NS_APPLIANCE_SYSTEM_ALL)).get(mc.KEY_PAYLOAD)
    payload.update((await client.async_request(mc.NS_APPLIANCE_SYSTEM_ABILITY)).get(mc.KEY_PAYLOAD))
    return {
        CONF_HOST: host,
        CONF_PAYLOAD: payload,
        CONF_KEY: key
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info: dict = None
    _device_id: str = None
    _host: str = None
    _key: str = None

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)


    async def async_step_user(self, user_input=None):

        errors = {}
        if user_input is None:
            # we could get here from user flow start in UI
            # or following dhcp discovery
            if self._host is None:
                # check we already configured the hub ..
                if (DOMAIN not in self._async_current_ids()) and mqtt_is_loaded(self.hass):
                    return await self.async_step_hub()
        else:
            self._host = user_input[CONF_HOST]
            self._key = user_input.get(CONF_KEY)
            try:
                _discovery_info = await _http_discovery(self._host, self._key, self.hass)
                return await self.async_step_discovery(_discovery_info)
            except Exception as e:
                LOGGER.debug("Error (%s) connecting to meross device (host:%s)", str(e), self._host)
                errors["base"] = "cannot_connect"

        config_schema = {
            vol.Required(CONF_HOST, description={"suggested_value": self._host}): str,
            vol.Optional(CONF_KEY, description={"suggested_value": self._key}): str,
            }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(config_schema), errors=errors)


    async def async_step_discovery(self, discovery_info: DiscoveryInfoType):
        await self._async_set_info(discovery_info)
        return await self.async_step_device()


    async def async_step_dhcp(self, discovery_info: DiscoveryInfoType):
        """Handle a flow initialized by DHCP discovery."""
        LOGGER.debug("received dhcp discovery: %s", json.dumps(discovery_info))

        self._host = discovery_info.get('ip')
        self._macaddress = discovery_info.get('macaddress')

        """
        we'll update the unique_id for the flow when we'll have the device_id
        macaddress would have been a better choice since the beginning (...)
        but I don't want to mess with ConfigEntry versioning right now
        Here this is needed in case we cannot correctly identify the device
        via our api and the dhcp integration keeps pushing us discoveries for
        the same device
        """
        await self.async_set_unique_id(self._macaddress, raise_on_progress=True)

        """
        Check we already dont have the device registered.
        This is probably overkill since the ConfigFlow will recognize
        the duplicated unique_id sooner or later
        """
        api = self.hass.data.get(DOMAIN)
        if api is not None:
            if api.has_device(self._host, self._macaddress):
                return self.async_abort(reason='already_configured')
            self._key = api.key

        try:
            # try device identification so the user/UI has a good context to start with
            _discovery_info = await _http_discovery(self._host, self._key, self.hass)
            await self._async_set_info(_discovery_info)
            # now just let the user edit/accept the host address even if identification was fine
        except Exception as e:
            LOGGER.debug("Error (%s) connecting to meross device (host:%s)", str(e), self._host)
            # forgive and continue if we cant discover the device...let the user work it out

        return await self.async_step_user()


    async def async_step_device(self, user_input=None):
        data = self._discovery_info

        if user_input is None:
            config_schema = {}
            return self.async_show_form(
                step_id="device",
                data_schema=vol.Schema(config_schema),
                description_placeholders=self._placeholders
            )

        return self.async_create_entry(title=self._descriptor.type + " " + self._device_id, data=data)


    async def async_step_hub(self, user_input=None):
        #right now this is only used to setup MQTT Hub feature to allow discovery and mqtt message sub/pub
        if user_input == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = { vol.Optional(CONF_KEY): str }
            return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))

        return self.async_create_entry(title="MQTT Hub", data=user_input)


    async def _async_set_info(self, discovery_info: DiscoveryInfoType) -> None:
        self._discovery_info = discovery_info
        self._descriptor = MerossDeviceDescriptor(discovery_info.get(CONF_PAYLOAD, {}))
        self._device_id = self._descriptor.uuid
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        discovery_info[CONF_TIME_ZONE] = self._descriptor.timezone
        if CONF_DEVICE_ID not in discovery_info:#this is coming from manual user entry or dhcp discovery
            discovery_info[CONF_DEVICE_ID] = self._device_id

        self._placeholders = {
            CONF_DEVICE_TYPE: get_productnametype(self._descriptor.type),
            CONF_DEVICE_ID: self._device_id,
            CONF_PAYLOAD: ""#json.dumps(data.get(CONF_PAYLOAD, {}))
            }

        self.context["title_placeholders"] = self._placeholders
        return


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

        if user_input is not None:
            data = dict(data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            data[CONF_PROTOCOL] = user_input.get(CONF_PROTOCOL)
            data[CONF_POLLING_PERIOD] = user_input.get(CONF_POLLING_PERIOD)
            data[CONF_TIME_ZONE] = user_input.get(CONF_TIME_ZONE)
            data[CONF_TRACE] = time() + CONF_TRACE_TIMEOUT if user_input.get(CONF_TRACE) else 0
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
        config_schema[
            vol.Optional(
                CONF_POLLING_PERIOD,
                default=CONF_POLLING_PERIOD_DEFAULT,
                description={"suggested_value": data.get(CONF_POLLING_PERIOD)}
                )
            ] = cv.positive_int
        config_schema[
            vol.Optional(
                CONF_TIME_ZONE,
                description={"suggested_value": data.get(CONF_TIME_ZONE)}
                )
            ] = vol.In(common_timezones) if common_timezones is not None else str
        config_schema[
            vol.Optional(
                CONF_TRACE,
                # CONF_TRACE contains the trace 'end' time epoch if set
                description={"suggested_value": data.get(CONF_TRACE, 0) > time()}
                )
            ] = bool

        descriptor = MerossDeviceDescriptor(data.get(CONF_PAYLOAD, {}))
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders={
                CONF_DEVICE_TYPE: get_productnametype(descriptor.type),
                CONF_DEVICE_ID: data.get(CONF_DEVICE_ID),
                CONF_HOST: data.get(CONF_HOST) or "MQTT",
                CONF_PAYLOAD: ""#json.dumps(data.get(CONF_PAYLOAD, {}))
            }
        )
