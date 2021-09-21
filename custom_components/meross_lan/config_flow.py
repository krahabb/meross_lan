"""Config flow for Meross IoT local LAN integration."""
from typing import OrderedDict
from time import time
from uuid import uuid4
from hashlib import md5
from base64 import b64encode
import async_timeout
import voluptuous as vol
import json

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .merossclient import (
    MerossHttpClient,
    MerossDeviceDescriptor,
    MerossKeyError,
    const as mc,
    get_productnametype
)
from . import MerossApi
from .meross_device import MerossDevice
from .helpers import LOGGER, mqtt_is_loaded
from .const import (
    DOMAIN,
    CONF_HOST, CONF_DEVICE_ID, CONF_KEY, CONF_CLOUD_KEY,
    CONF_PAYLOAD, CONF_DEVICE_TYPE,
    CONF_PROTOCOL, CONF_PROTOCOL_OPTIONS,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT,
    CONF_TRACE, CONF_TRACE_TIMEOUT,
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    _discovery_info: dict = None
    _device_id: str = None
    _host: str = None
    _key: str = None
    _cloud_key: str = None
    _keyerror: bool = False

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
            if self._host is None:# it means it's not dhcp discovery
                # check we already configured the hub ..
                if (DOMAIN not in self._async_current_ids()) and mqtt_is_loaded(self.hass):
                    return await self.async_step_hub()
                # user starting a flow then: fill up the key if any
                api = self.hass.data.get(DOMAIN)
                if api is not None:
                    if api.cloud_key is not None:
                        self._key = api.cloud_key
                        self._cloud_key = api.cloud_key
        else:
            _host = user_input[CONF_HOST]
            _key = user_input.get(CONF_KEY)

            if self._keyerror: #previous attempt failed
                self._keyerror = False #reset
                if (_host == self._host) and (_key == self._key):
                    #if user didn't modified the data forward to cloud key retrieval
                    return await self.async_step_retrievekey()

            self._host = _host
            self._key = _key
            try:
                _discovery_info = await self._http_discovery()
                return await self.async_step_discovery(_discovery_info)
            except MerossKeyError as e:
                errors["base"] = "invalid_key"
                self._keyerror = True
            except AbortFlow as e:
                errors["base"] = "already_configured_device"
            except Exception as e:
                LOGGER.warning("Error (%s) configuring meross device (host:%s)", str(e), self._host)
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

        try:
            # try device identification so the user/UI has a good context to start with
            _discovery_info = None
            if api is not None:
                """
                we'll see if any previous device already used a 'cloud_key' retrieved
                from meross api for cloud-paired devices and try it
                """
                if api.cloud_key is not None:
                    self._key = api.cloud_key
                    try:
                        _discovery_info = await self._http_discovery()
                        self._cloud_key = self._key # pass along so we'll save it in this entry too
                    except MerossKeyError:
                        pass
                if (_discovery_info is None) and (api.key is not None):
                    self._key = api.key
                    try:
                        _discovery_info = await self._http_discovery()
                    except MerossKeyError:
                        pass
            if _discovery_info is None:
                self._key = None # no other options: try empty key (which will eventually use the reply-hack)
                _discovery_info = await self._http_discovery()
            await self._async_set_info(_discovery_info)
            # now just let the user edit/accept the host address even if identification was fine
        except Exception as e:
            LOGGER.debug("Error (%s) identifying meross device (host:%s)", str(e), self._host)
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


    async def async_step_retrievekey(self, user_input=None):
        errors = {}

        if user_input:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                timestamp = int(time())
                nonce = uuid4().hex
                params = '{"email": "'+username+'", "password": "'+password+'"}'
                params = b64encode(params.encode('utf-8')).decode('ascii')
                sign = md5(("23x17ahWarFH6w29" + str(timestamp) + nonce + params).encode('utf-8')).hexdigest()
                with async_timeout.timeout(10):
                    response = await async_get_clientsession(self.hass).post(
                        url=mc.MEROSS_API_LOGIN_URL,
                        json={
                            mc.KEY_TIMESTAMP: timestamp,
                            mc.KEY_NONCE: nonce,
                            mc.KEY_PARAMS: params,
                            mc.KEY_SIGN: sign
                        }
                    )
                    response.raise_for_status()
                json: dict = await response.json()
                self._cloud_key = json.get(mc.KEY_DATA, {}).get(mc.KEY_KEY)
                self._key = self._cloud_key
                return await self.async_step_user()
            except Exception as e:
                errors["base"] = "cannot_connect"

        else:
            username = ""
            password = ""

        return self.async_show_form(
            step_id="retrievekey",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, description={ "suggested_value" : username }): str,
                vol.Required(CONF_PASSWORD, description={ "suggested_value" : password }): str
                }),
            errors=errors
            )


    async def _async_set_info(self, discovery_info: DiscoveryInfoType) -> None:
        self._discovery_info = discovery_info
        self._descriptor = MerossDeviceDescriptor(discovery_info.get(CONF_PAYLOAD, {}))
        self._device_id = self._descriptor.uuid
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        if CONF_DEVICE_ID not in discovery_info:#this is coming from manual user entry or dhcp discovery
            discovery_info[CONF_DEVICE_ID] = self._device_id

        if (self._cloud_key is not None) and (self._cloud_key == self._key):
            # save (only if good) so we can later automatically retrieve for new devices
            discovery_info[CONF_CLOUD_KEY] = self._cloud_key
        else:
            discovery_info.pop(CONF_CLOUD_KEY, None)

        self._placeholders = {
            CONF_DEVICE_TYPE: get_productnametype(self._descriptor.type),
            CONF_DEVICE_ID: self._device_id,
            CONF_PAYLOAD: ""#json.dumps(data.get(CONF_PAYLOAD, {}))
            }

        self.context["title_placeholders"] = self._placeholders
        return


    async def _http_discovery(self) -> dict:
        client = MerossHttpClient(self._host, self._key, async_get_clientsession(self.hass), LOGGER)
        payload: dict = (await client.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ALL))[mc.KEY_PAYLOAD]
        payload.update((await client.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ABILITY))[mc.KEY_PAYLOAD])
        return {
            CONF_HOST: self._host,
            CONF_PAYLOAD: payload,
            CONF_KEY: self._key
        }


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
        device_id = data.get(CONF_DEVICE_ID)
        device: MerossDevice = None
        api: MerossApi = self.hass.data.get(DOMAIN)
        if api is not None:
            device: MerossDevice = api.devices.get(device_id)

        if user_input is not None:
            data = dict(data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            data[CONF_PROTOCOL] = user_input.get(CONF_PROTOCOL)
            data[CONF_POLLING_PERIOD] = user_input.get(CONF_POLLING_PERIOD)
            data[CONF_TRACE] = time() + CONF_TRACE_TIMEOUT if user_input.get(CONF_TRACE) else 0
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            if device is not None:
                try:
                    device.entry_option_update(user_input)
                except:
                    pass # forgive any error
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

        # setup device specific config right before last option
        if device is not None:
            try:
                device.entry_option_setup(config_schema)
            except:
                pass # forgive any error

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
                CONF_DEVICE_ID: device_id,
                CONF_HOST: data.get(CONF_HOST) or "MQTT",
                CONF_PAYLOAD: ""#json.dumps(data.get(CONF_PAYLOAD, {}))
            }
        )
