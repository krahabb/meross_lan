"""Config flow for Meross IoT local LAN integration."""
from time import time
from logging import DEBUG
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components import dhcp

from .merossclient import (
    MerossHttpClient,
    MerossDeviceDescriptor,
    MerossKeyError,
    const as mc,
    get_productnametype, async_get_cloud_key,
)
from . import MerossApi
from .meross_device import MerossDevice
from .helpers import LOGGER, mqtt_is_loaded
from .const import (
    DOMAIN,
    CONF_HOST, CONF_DEVICE_ID, CONF_KEY, CONF_CLOUD_KEY,
    CONF_PAYLOAD,
    CONF_PROTOCOL, CONF_PROTOCOL_OPTIONS,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT,
    CONF_TRACE, CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT,
)

# helper conf keys not persisted to config
CONF_DEVICE_TYPE = 'device_type'
CONF_KEYMODE = 'keymode'
CONF_KEYMODE_USER = 'user'
CONF_KEYMODE_HACK = 'hack'
CONF_KEYMODE_CLOUDRETRIEVE = 'cloud'
CONF_KEYMODE_OPTIONS = {
    CONF_KEYMODE_USER: "User set",
    CONF_KEYMODE_HACK: "Hack mode",
    CONF_KEYMODE_CLOUDRETRIEVE: "Cloud retrieve",
}

DESCR = 'suggested_value'


async def _http_discovery(hass, host, key) -> dict:
    c = MerossHttpClient(host, key, async_get_clientsession(hass), LOGGER)
    payload: dict = (await c.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ALL))[mc.KEY_PAYLOAD]
    payload.update((await c.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ABILITY))[mc.KEY_PAYLOAD])
    return {
        CONF_HOST: host,
        CONF_PAYLOAD: payload,
        CONF_KEY: key
    }


class ConfigError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class CloudKeyMixin:

    async def async_step_cloudkey(self, user_input=None):
        errors = {}

        if user_input:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                self._cloud_key = await async_get_cloud_key(
                    username, password, async_get_clientsession(self.hass))
                self._key = self._cloud_key
                return await self.async_step_device()
            except Exception as e:
                errors["base"] = "cannot_connect"

        else:
            username = ""
            password = ""

        return self.async_show_form(
            step_id="cloudkey",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, description={ DESCR: username }): str,
                vol.Required(CONF_PASSWORD, description={ DESCR: password }): str
                }),
            errors=errors)



class ConfigFlow(CloudKeyMixin, config_entries.ConfigFlow, domain=DOMAIN):
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
        """
        This is the entry point for user initiated flows
        it is just 'smart' a selector for the proper config step
        """
        # check we already configured the hub ..
        if (DOMAIN not in self._async_current_ids()) and mqtt_is_loaded(self.hass):
            return await self.async_step_hub()
        # user starting a flow then: fill up the key if any
        if (api := self.hass.data.get(DOMAIN)) is not None:
            if api.cloud_key is not None:
                self._key = api.cloud_key
                self._cloud_key = api.cloud_key
        return await self.async_step_device()


    async def async_step_hub(self, user_input=None):
        #right now this is only used to setup MQTT Hub feature to allow discovery and mqtt message sub/pub
        if user_input == None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = { vol.Optional(CONF_KEY): str }
            return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))
        return self.async_create_entry(title="MQTT Hub", data=user_input)


    async def async_step_device(self, user_input=None):

        errors = {}

        if user_input is None:
            _keymode = CONF_KEYMODE_CLOUDRETRIEVE
        else:
            self._host = user_input[CONF_HOST]
            self._key = user_input.get(CONF_KEY)
            _keymode = user_input[CONF_KEYMODE]
            """
            The user might think that he needs to set cloudretrieve
            even after having already retrieved the cloud key
            this code safely skip re-routing again to async_step_cloudkey
            """
            try:
                if self._key is None:
                    if _keymode == CONF_KEYMODE_CLOUDRETRIEVE:
                        return await self.async_step_cloudkey()
                    elif _keymode != CONF_KEYMODE_HACK:
                        raise ConfigError("invalid_nullkey")
                """
                when the key is non-null we'll give it a try and eventually
                go to cloud retrieve if the user selected so
                """
                _discovery_info = await _http_discovery(self.hass, self._host, self._key)
                await self._async_set_info(_discovery_info)
                return await self.async_step_finalize()
            except ConfigError as e:
                errors["base"] = e.reason
            except MerossKeyError as e:
                self._cloud_key = None
                if _keymode == CONF_KEYMODE_CLOUDRETRIEVE:
                    return await self.async_step_cloudkey()
                errors["base"] = "invalid_key"
            except AbortFlow as e:
                errors["base"] = "already_configured_device"
            except Exception as e:
                LOGGER.warning("Error (%s) configuring meross device (host:%s)", str(e), self._host)
                errors["base"] = "cannot_connect"

        config_schema = {
            vol.Required(CONF_HOST, description={ DESCR: self._host}): str,
            vol.Required(CONF_KEYMODE, description={ DESCR: _keymode }): vol.In(CONF_KEYMODE_OPTIONS),
            vol.Optional(CONF_KEY, description={ DESCR: self._key}): str,
            }
        return self.async_show_form(step_id="device", data_schema=vol.Schema(config_schema), errors=errors)


    async def async_step_discovery(self, discovery_info: DiscoveryInfoType):
        """
        this is actually the entry point for devices discovered through our mqtt hub
        """
        await self._async_set_info(discovery_info)
        return await self.async_step_finalize()


    async def async_step_dhcp(self, discovery_info: object):
        """Handle a flow initialized by DHCP discovery."""
        if LOGGER.isEnabledFor(DEBUG):
            LOGGER.debug("received dhcp discovery: %s", str(discovery_info))

        try:# not sure when discovery_info signature changed...we'll play it safe
            if isinstance(discovery_info, dhcp.DhcpServiceInfo):
                self._host = discovery_info.ip
                self._macaddress = discovery_info.macaddress
        except:
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
                LOGGER.debug("ignoring dhcp discovery for %s: already configured", self._host)
                return self.async_abort(reason='already_configured')

        try:
            # try device identification so the user/UI has a good context to start with
            if api is not None:
                """
                we'll see if any previous device already used a 'cloud_key' retrieved
                from meross api for cloud-paired devices and try it
                """
                _discovery_info = None
                if api.cloud_key is not None:
                    try:
                        _discovery_info = await _http_discovery(self.hass, self._host, api.cloud_key)
                        self._key = api.cloud_key
                        self._cloud_key = api.cloud_key # pass along so we'll save it in this entry too
                    except MerossKeyError:
                        pass
                if (_discovery_info is None) and (api.key is not None):
                    try:
                        _discovery_info = await _http_discovery(self.hass, self._host, api.key)
                        self._key = api.key
                    except MerossKeyError:
                        pass
                if _discovery_info is not None:
                    await self._async_set_info(_discovery_info)
            """
            we're now skipping key-hack discovery since devices on recent firmware
            look like they really hate this hack...
            if _discovery_info is None:
                self._key = None # no other options: try empty key (which will eventually use the reply-hack)
                _discovery_info = await self._http_discovery()
            await self._async_set_info(_discovery_info)
            """
            # now just let the user edit/accept the host address even if identification was fine
        except Exception as e:
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug("Error (%s) identifying meross device (host:%s)", str(e), self._host)
            if isinstance(e, AbortFlow):
                # we might have 'correctly' identified an already configured entry
                return self.async_abort(reason='already_configured')
            # forgive and continue if we cant discover the device...let the user work it out

        return await self.async_step_device()


    async def async_step_finalize(self, user_input=None):
        data = self._discovery_info
        if user_input is None:
            config_schema = {}
            return self.async_show_form(
                step_id="finalize",
                data_schema=vol.Schema(config_schema),
                description_placeholders=self._placeholders
            )
        return self.async_create_entry(title=self._descriptor.type + " " + self._device_id, data=data)


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



class OptionsFlowHandler(CloudKeyMixin, config_entries.OptionsFlow):
    """
        Manage device options configuration
    """

    def __init__(self, config_entry):
        self._config_entry = config_entry
        if config_entry.unique_id != DOMAIN:
            data = config_entry.data
            self.device_id = data.get(CONF_DEVICE_ID)
            self._host = data.get(CONF_HOST) # null for devices discovered over mqtt
            self._key = data.get(CONF_KEY)
            self._cloud_key = data.get(CONF_CLOUD_KEY) # null for non cloud keys
            self._protocol = data.get(CONF_PROTOCOL)
            self._polling_period = data.get(CONF_POLLING_PERIOD)
            self._trace = data.get(CONF_TRACE, 0) > time()
            self._trace_timeout = data.get(CONF_TRACE_TIMEOUT)


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

        config_schema = {
            vol.Optional(
                CONF_KEY,
                description={ DESCR: self._config_entry.data.get(CONF_KEY) }
                ): str
        }

        return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))


    async def async_step_device(self, user_input=None):
        errors = {}

        api: MerossApi = self.hass.data.get(DOMAIN)
        device: MerossDevice = api.devices.get(self.device_id)

        if user_input is None:
            # this preset will force (if _host) a connection check
            # and start the cloud retrieval in case the key fails
            # whatever the actual key is (None or set)
            # this is the default but the user could anyway choose KEYMODE_HACK or so
            # but he has to do that consciously
            _keymode = CONF_KEYMODE_CLOUDRETRIEVE
        else:
            self._host = user_input.get(CONF_HOST)
            self._key = user_input.get(CONF_KEY)
            self._protocol = user_input.get(CONF_PROTOCOL)
            self._polling_period = user_input.get(CONF_POLLING_PERIOD)
            self._trace = user_input.get(CONF_TRACE)
            self._trace_timeout = user_input.get(CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT)
            try:
                if self._host is not None:
                    _keymode = user_input[CONF_KEYMODE]
                    if self._key is None:
                        if _keymode == CONF_KEYMODE_CLOUDRETRIEVE:
                            return await self.async_step_cloudkey()
                        elif _keymode != CONF_KEYMODE_HACK:
                            raise ConfigError("invalid_nullkey")
                    _discovery_info = await _http_discovery(self.hass, self._host, self._key)
                    _descriptor = MerossDeviceDescriptor(_discovery_info.get(CONF_PAYLOAD, {}))
                    if self.device_id != _descriptor.uuid:
                        raise ConfigError("device_id_mismatch")

                data = dict(self._config_entry.data)
                if self._host is not None:
                    data[CONF_HOST] = self._host
                    if self._cloud_key and (self._cloud_key == self._key):
                        data[CONF_CLOUD_KEY] = self._cloud_key
                    else:
                        data.pop(CONF_CLOUD_KEY, None)
                data[CONF_KEY] = self._key
                data[CONF_PROTOCOL] = self._protocol
                data[CONF_POLLING_PERIOD] = self._polling_period
                data[CONF_TRACE] = (time() + self._trace_timeout) if self._trace else 0
                data[CONF_TRACE_TIMEOUT] = self._trace_timeout
                self.hass.config_entries.async_update_entry(self._config_entry, data=data)
                if device is not None:
                    try:
                        device.entry_option_update(user_input)
                    except:
                        pass # forgive any error
                return self.async_create_entry(title=None, data=None)

            except MerossKeyError as e:
                self._cloud_key = None
                if _keymode == CONF_KEYMODE_CLOUDRETRIEVE:
                    return await self.async_step_cloudkey()
                errors["base"] = "invalid_key"
            except ConfigError as e:
                errors["base"] = e.reason
            except Exception as e:
                errors["base"] = "cannot_connect"

        config_schema = dict()
        if self._host is not None:
            config_schema[
                vol.Required(
                    CONF_HOST,
                    description={ DESCR: self._host}
                    )
                ] = str
            config_schema[
                vol.Required(
                    CONF_KEYMODE,
                    description={ DESCR: _keymode}
                    )
                ] = vol.In(CONF_KEYMODE_OPTIONS)
        config_schema[
            vol.Optional(
                CONF_KEY,
                description={ DESCR: self._key}
                )
            ] = str
        config_schema[
            vol.Optional(
                CONF_PROTOCOL,
                description={ DESCR: self._protocol}
                )
            ] = vol.In(CONF_PROTOCOL_OPTIONS)
        config_schema[
            vol.Optional(
                CONF_POLLING_PERIOD,
                default=CONF_POLLING_PERIOD_DEFAULT,
                description={ DESCR: self._polling_period}
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
                description={ DESCR: self._trace}
                )
            ] = bool
        config_schema[
            vol.Optional(
                CONF_TRACE_TIMEOUT,
                default=CONF_TRACE_TIMEOUT_DEFAULT,
                description={ DESCR: self._trace_timeout}
                )
            ] = cv.positive_int


        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders={
                CONF_DEVICE_TYPE: get_productnametype(device.descriptor.type) if device is not None else "",
                CONF_DEVICE_ID: self.device_id,
                CONF_HOST: self._host or "MQTT",
                CONF_PAYLOAD: ""#json.dumps(data.get(CONF_PAYLOAD, {}))
            },
            errors=errors
        )
