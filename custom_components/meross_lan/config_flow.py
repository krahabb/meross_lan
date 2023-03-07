"""Config flow for Meross LAN integration."""
from __future__ import annotations
import typing
from time import time
from logging import DEBUG
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowHandler, AbortFlow
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_ERROR,
)
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .merossclient import (
    const as mc,
    KeyType,
    MerossDeviceDescriptor,
    MerossKeyError,
    get_default_arguments,
)
from .merossclient.httpclient import MerossHttpClient
from .merossclient.cloudapi import (
    CloudApiError,
    async_cloudapi_login,
)

from . import MerossApi
from .helpers import LOGGER
from .const import (
    DOMAIN,
    DeviceConfigType,
    CONF_HOST,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_CLOUD_KEY,
    CONF_CLOUD_PROFILE_ID,
    CONF_PAYLOAD,
    CONF_TIMESTAMP,
    CONF_PROTOCOL,
    CONF_PROTOCOL_OPTIONS,
    CONF_POLLING_PERIOD,
    CONF_POLLING_PERIOD_DEFAULT,
    CONF_TRACE,
    CONF_TRACE_TIMEOUT,
    CONF_TRACE_TIMEOUT_DEFAULT,
)

# helper conf keys not persisted to config
CONF_DEVICE_TYPE = "device_type"
DESCR = "suggested_value"
ERR_BASE = "base"
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_INVALID_KEY = "invalid_key"
ERR_INVALID_NULL_KEY = "invalid_nullkey"
ERR_DEVICE_ID_MISMATCH = "device_id_mismatch"
ERR_ALREADY_CONFIGURED_DEVICE = "already_configured_device"
ERR_INVALID_AUTH = "invalid_auth"
ERR_CLOUD_PROFILE_MISMATCH = "cloud_profile_mismatch"


class ConfigError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MerossFlowHandlerMixin(FlowHandler if typing.TYPE_CHECKING else object):
    """Mixin providing commons for Config and Option flows"""

    _device_id: str | None = None
    _host: str | None = None
    _key: str | None = None
    _cloud_profile_id: str | None = None
    _placeholders = {
        CONF_DEVICE_TYPE: "",
        CONF_DEVICE_ID: "",
    }

    _httpclient: MerossHttpClient | None = None

    def show_keyerror(self):
        return self.async_show_menu(
            step_id="keyerror", menu_options=["cloudkey", "device"]
        )

    async def async_step_cloudkey(self, user_input=None):
        """manage the cloud login form to retrieve the device key"""
        errors = {}

        if user_input:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                credentials = await async_cloudapi_login(
                    username, password, async_get_clientsession(self.hass)
                )
                profile = await MerossApi.async_update_profile(self.hass, credentials)
                self._key = profile.key
                self._cloud_profile_id = profile.profile_id
                return await self.async_step_device()  # type: ignore
            except CloudApiError as error:
                errors[CONF_ERROR] = ERR_INVALID_AUTH
                _err = str(error)
            except Exception as error:
                errors[CONF_ERROR] = ERR_CANNOT_CONNECT
                _err = str(error) or type(error).__name__

            return self.async_show_form(
                step_id="cloudkey",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_USERNAME, description={DESCR: username}): str,
                        vol.Required(CONF_PASSWORD, description={DESCR: password}): str,
                        vol.Optional(CONF_ERROR, description={DESCR: _err}): str,
                    }
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="cloudkey",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
        )

    async def _async_http_discovery(self, key: str | None) -> DeviceConfigType:
        # passing key=None would allow key-hack and we don't want it aymore
        if key is None:
            key = ""
        if (_httpclient := self._httpclient) is None:
            _httpclient = self._httpclient = MerossHttpClient(
                self._host, key, async_get_clientsession(self.hass), LOGGER # type: ignore
            )
        else:
            _httpclient.host = self._host # type: ignore
            _httpclient.key = key

        payload = (
            await _httpclient.async_request_strict(
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
            )
        )[mc.KEY_PAYLOAD]
        payload.update(
            (
                await _httpclient.async_request_strict(
                    *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY)
                )
            )[mc.KEY_PAYLOAD]
        )
        return {CONF_HOST: self._host, CONF_PAYLOAD: payload, CONF_KEY: key}


class ConfigFlow(MerossFlowHandlerMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    VERSION = 1

    _device_config: DeviceConfigType | None = None

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        if (api := MerossApi.peek(self.hass)) is not None:
            if (profile := next(iter(api.profiles.values()), None)) is not None:
                self._key = profile.key
                self._cloud_profile_id = profile.profile_id
        return await self.async_step_device()

    async def async_step_hub(self, user_input=None):
        """configure the MQTT discovery device key"""
        if user_input is None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = {vol.Optional(CONF_KEY): str}
            return self.async_show_form(
                step_id="hub", data_schema=vol.Schema(config_schema)
            )
        return self.async_create_entry(title="MQTT Hub", data=user_input)

    async def async_step_device(self, user_input=None):
        """common device configuration"""
        errors = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._key = user_input.get(CONF_KEY)
            try:
                await self._async_set_device_config(
                    await self._async_http_discovery(self._key)
                )
                return self.show_finalize()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except MerossKeyError:
                return self.show_keyerror()
            except AbortFlow:
                errors[ERR_BASE] = ERR_ALREADY_CONFIGURED_DEVICE
            except Exception as error:
                LOGGER.warning(
                    "Error (%s) configuring meross device (host:%s)",
                    str(error),
                    self._host,
                )
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {
            vol.Required(CONF_HOST, description={DESCR: self._host}): str,
            vol.Optional(CONF_KEY, description={DESCR: self._key}): str,
        }
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            errors=errors,
            description_placeholders=self._placeholders,
        )

    async def async_step_discovery(self, discovery_info: DeviceConfigType):
        """
        this is actually the entry point for devices discovered through our mqtt hub
        """
        await self._async_set_device_config(discovery_info)
        return self.show_finalize()

    async def async_step_dhcp(self, discovery_info):
        """Handle a flow initialized by DHCP discovery."""
        if LOGGER.isEnabledFor(DEBUG):
            LOGGER.debug("received dhcp discovery: %s", str(discovery_info))
        self._host = discovery_info.ip
        macaddress = discovery_info.macaddress.replace(":", "").lower()
        # check if the device is already registered
        try:
            entries = self.hass.config_entries
            for entry in entries.async_entries(DOMAIN):
                descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD))
                if descriptor.macAddress.replace(":", "").lower() != macaddress:
                    continue
                data = dict(entry.data)
                data[CONF_HOST] = self._host
                data[CONF_TIMESTAMP] = time()  # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
                LOGGER.info(
                    "DHCP updated device ip address (%s) for device %s",
                    self._host,
                    descriptor.uuid,
                )
                return self.async_abort(reason="already_configured")
        except Exception as error:
            LOGGER.warning("DHCP update internal error: %s", str(error))
        # we'll update the unique_id for the flow when we'll have the device_id
        # Here this is needed in case we cannot correctly identify the device
        # via our api and the dhcp integration keeps pushing us discoveries for
        # the same device
        # update 2022-12-19: adding DOMAIN prefix since macaddress alone might be set by other
        # integrations and that would conflict with our unique_id likely raising issues
        # on DHCP discovery not working in some configurations
        await self.async_set_unique_id(DOMAIN + macaddress, raise_on_progress=True)
        # Check we already dont have the device registered.
        # This is a legacy (dead code) check since we've already
        # looped through our config entries and updated the ip address there
        api = MerossApi.get(self.hass)
        if api.get_device_with_mac(macaddress) is not None:
            return self.async_abort(reason="already_configured")
        try:
            # try device identification so the user/UI has a good context to start with
            _device_config = None
            for profile in api.profiles.values():
                try:
                    _device_config = await self._async_http_discovery(profile.key)
                    # deeply check the device is really bounded to the profile
                    # since the key might luckily be good even tho the profile not
                    _descriptor = MerossDeviceDescriptor(_device_config[CONF_PAYLOAD])
                    if _descriptor.userId == profile.profile_id:
                        self._key = profile.key
                        self._cloud_profile_id = profile.profile_id
                        break
                except:
                    pass
                _device_config = None

            if (_device_config is None) and ((key := api.key) is not None):
                try:
                    _device_config = await self._async_http_discovery(key)
                    self._key = key
                except:
                    pass

            if _device_config is not None:
                await self._async_set_device_config(_device_config)

        except Exception as error:
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug(
                    "Error (%s) identifying meross device (host:%s)",
                    str(error),
                    self._host,
                )
            if isinstance(error, AbortFlow):
                # we might have 'correctly' identified an already configured entry
                return self.async_abort(reason="already_configured")
            # forgive and continue if we cant discover the device...let the user work it out

        return await self.async_step_device()

    async def async_step_mqtt(self, discovery_info):
        """manage the MQTT discovery flow"""
        await self.async_set_unique_id(DOMAIN)
        # this entry should only ever called once after startup
        # when HA thinks we're interested in discovery.
        # If our MerossApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        api = MerossApi.get(self.hass)
        if api.mqtt_is_subscribed():
            return self.async_abort(reason="already_configured")
        # try setup the mqtt subscription
        # this call might not register because of errors or because of an overlapping
        # request from 'async_setup_entry' (we're preventing overlapped calls to MQTT
        # subscription)
        await api.async_mqtt_register()
        if api.mqtt_is_subscribed():
            # ok, now pass along the discovering mqtt message so our MerossApi state machine
            # gets to work on this
            await api.async_mqtt_message(discovery_info)
        # just in case, setup the MQTT Hub entry to enable the (default) device key configuration
        # if the entry hub is already configured this will disable the discovery
        # subscription (by returning 'already_configured') stopping any subsequent async_step_mqtt message:
        # our MerossApi should already be in place
        return await self.async_step_hub()

    def show_finalize(self):
        """just a recap form"""
        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
        )

    async def async_step_finalize(self, user_input=None):
        return self.async_create_entry(
            title=f"{self._descriptor.type} {self._device_id}",
            data=self._device_config,  # type: ignore
        )

    async def _async_set_device_config(self, data: DeviceConfigType):
        self._device_config = data
        self._descriptor = MerossDeviceDescriptor(data.get(CONF_PAYLOAD))
        self._device_id = self._descriptor.uuid
        self._placeholders = {
            CONF_DEVICE_TYPE: self._descriptor.productnametype,
            CONF_DEVICE_ID: self._device_id,
        }
        self.context["title_placeholders"] = self._placeholders
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        if CONF_DEVICE_ID not in data:
            # this is coming from manual user entry or dhcp discovery
            data[CONF_DEVICE_ID] = self._device_id


class OptionsFlowHandler(MerossFlowHandlerMixin, config_entries.OptionsFlow):
    """
    Manage device options configuration
    """
    _trace: bool # this is the UI value (yes or no) CONF_TRACE carries endtime

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry
        if config_entry.unique_id != DOMAIN:
            data: DeviceConfigType = config_entry.data # type: ignore
            self._device_id = data.get(CONF_DEVICE_ID)
            self._host = data.get(CONF_HOST)  # null for devices discovered over mqtt
            self._key = data.get(CONF_KEY)
            self._cloud_profile_id = data.get(CONF_CLOUD_PROFILE_ID)
            self._protocol = data.get(CONF_PROTOCOL)
            self._polling_period = data.get(CONF_POLLING_PERIOD)
            self._trace = (data.get(CONF_TRACE) or 0) > time()
            self._trace_timeout = data.get(CONF_TRACE_TIMEOUT)
            self._placeholders = {
                CONF_DEVICE_ID: self._device_id,
                CONF_HOST: self._host or "MQTT",
            }
            # since the introduction of cloud_profile, the cloud_key
            # is not used anymore and we just check if a device
            # has not been migrated yet to propose a correct configuration
            # device migration is done once the user confirms the OptionsFlow
            # so this code needs to last a bit(indefinitely?)
            if not self._cloud_profile_id:
                # this means either the device is not migrated or
                # is locally bound.
                if cloud_key := data.get(CONF_CLOUD_KEY):
                    # not migrated and supposedly good cloud_key
                    # suggest a profile
                    api = MerossApi.get(self.hass)
                    if (profile := api.get_profile_by_key(cloud_key)) is not None:
                        self._cloud_profile_id = profile.profile_id




    async def async_step_init(self, user_input=None):
        if self._config_entry.unique_id == DOMAIN:
            return await self.async_step_hub(user_input)
        return await self.async_step_device(user_input)

    async def async_step_hub(self, user_input=None):

        if user_input is not None:
            data = dict(self._config_entry.data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(title="", data=None)  # type: ignore

        config_schema = {
            vol.Optional(
                CONF_KEY, description={DESCR: self._config_entry.data.get(CONF_KEY)}
            ): str
        }
        return self.async_show_form(
            step_id="hub", data_schema=vol.Schema(config_schema)
        )

    async def async_step_device(self, user_input: DeviceConfigType | None = None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        errors = {}
        api = MerossApi.get(self.hass)
        device = api.devices[self._device_id] # type:ignore
        if user_input is not None:
            self._host = user_input.get(CONF_HOST)
            self._key = user_input.get(CONF_KEY)
            self._protocol = user_input.get(CONF_PROTOCOL)
            self._cloud_profile_id = user_input.get(CONF_CLOUD_PROFILE_ID)
            self._polling_period = user_input.get(CONF_POLLING_PERIOD)
            self._trace = user_input.get(CONF_TRACE) # type: ignore
            self._trace_timeout = user_input.get(
                CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT
            )
            try:
                if self._host is None:
                    # this device has been discovered by mqtt and has no http
                    # reachability in config..we still lack a lot of stuff
                    # to fix this but this should be a less common scenario since
                    # most of the users should have added devices discovered on http
                    # which would be treated in the other branch
                    # TODO: implement mqtt connection check and validation
                    _descriptor = device.descriptor
                else:
                    _device_config = await self._async_http_discovery(self._key)
                    _descriptor = MerossDeviceDescriptor(_device_config[CONF_PAYLOAD])
                    if self._device_id != _descriptor.uuid:
                        raise ConfigError(ERR_DEVICE_ID_MISMATCH)
                if self._cloud_profile_id:
                    if (
                        profile := api.get_profile_by_id(self._cloud_profile_id)
                    ) is None:
                        # this shouldnt really happen since the UI list
                        # comes from actual api.profiles ....
                        raise ConfigError(ERR_CLOUD_PROFILE_MISMATCH)
                    if _descriptor.userId != self._cloud_profile_id:
                        raise ConfigError(ERR_CLOUD_PROFILE_MISMATCH)
                    if profile.key != self._key:
                        # TODO: treat the key mismatch in a different way:
                        # either raise a different error or fix the self._key
                        raise ConfigError(ERR_CLOUD_PROFILE_MISMATCH)
                data = dict(self._config_entry.data)
                if self._host is not None:
                    data[CONF_HOST] = self._host
                    data[CONF_PAYLOAD] = _device_config[CONF_PAYLOAD] # type: ignore

                data[CONF_KEY] = self._key
                data[CONF_PROTOCOL] = self._protocol
                data[CONF_CLOUD_PROFILE_ID] = self._cloud_profile_id
                data.pop(CONF_CLOUD_KEY, None)
                data[CONF_POLLING_PERIOD] = self._polling_period
                if self._trace:
                    data[CONF_TRACE] = time() + (self._trace_timeout or CONF_TRACE_TIMEOUT_DEFAULT)
                else:
                    data.pop(CONF_TRACE, None)
                data[CONF_TRACE_TIMEOUT] = self._trace_timeout
                try:
                    device.entry_option_update(user_input)
                except:
                    pass  # forgive any error
                # we're not following HA 'etiquette' and we're just updating the
                # config_entry data with this dirty trick
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=data
                )
                # return None in data so the async_update_entry is not called for the
                # options to be updated
                return self.async_create_entry(title=None, data=None)  # type: ignore

            except MerossKeyError:
                return self.show_keyerror()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except Exception:
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {}
        if self._host is not None:
            config_schema[
                vol.Required(CONF_HOST, description={DESCR: self._host})
            ] = str
        config_schema[vol.Optional(CONF_KEY, description={DESCR: self._key})] = str
        config_schema[
            vol.Optional(CONF_PROTOCOL, description={DESCR: self._protocol})
        ] = vol.In(CONF_PROTOCOL_OPTIONS.keys())
        config_schema[
            vol.Optional(
                CONF_CLOUD_PROFILE_ID, description={DESCR: self._cloud_profile_id}
            )
        ] = vol.In(api.get_profiles_map())
        config_schema[
            vol.Optional(
                CONF_POLLING_PERIOD,
                default=CONF_POLLING_PERIOD_DEFAULT,  # type: ignore
                description={DESCR: self._polling_period},
            )
        ] = cv.positive_int
        # setup device specific config right before last option
        self._placeholders[CONF_DEVICE_TYPE] = device.descriptor.productnametype
        try:
            device.entry_option_setup(config_schema)
        except:
            pass  # forgive any error

        config_schema[
            vol.Optional(
                CONF_TRACE,
                # CONF_TRACE contains the trace 'end' time epoch if set
                description={DESCR: self._trace},
            )
        ] = bool
        config_schema[
            vol.Optional(
                CONF_TRACE_TIMEOUT,
                default=CONF_TRACE_TIMEOUT_DEFAULT,  # type: ignore
                description={DESCR: self._trace_timeout},
            )
        ] = cv.positive_int

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders=self._placeholders,
            errors=errors,
        )
